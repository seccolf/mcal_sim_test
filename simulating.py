import logging
import shutil
import tempfile
import os

import numpy as np
import yaml
import joblib
import galsim
import fitsio
from esutil.ostools import StagedOutFile

from files import (
    get_band_info_file,
    make_dirs_for_file,
    get_truth_catalog_path,
    expand_path)
from constants import MEDSCONF
from truthing import make_coadd_grid_radec
from sky_bounding import get_rough_sky_bounds, radec_to_uv
from wcsing import get_esutil_wcs, get_galsim_wcs
from galsiming import render_sources_for_image
from psf_wrapper import PSFWrapper
from realistic_galaxying import init_descwl_catalog, get_descwl_galaxy

logger = logging.getLogger(__name__)

TMP_DIR = os.environ['TMPDIR']

class End2EndSimulation(object):
    """An end-to-end DES Y3 simulation.

    Parameters
    ----------
    seed : int
        The seed for the global RNG.
    output_meds_dir : str
        The output DEADATA/MEDS_DIR for the simulation data products.
    tilename : str
        The DES coadd tile to simulate.
    bands : str
        The bands to simulate.
    gal_kws : dict
        Keyword arguments to control the galaxy content of the simulation.
        Right now these should include:
            n_grid : int
                The galaxies will be put on a grid with `n_grid`
                on a side.
            g1 : float
                The true shear on the one-axis.
            g2 : float
                The true shear on the two-axis.
    psf_kws : dict
        Kyword arguments to control the PSF used for the simulation.
        Right now these should include:
            type : str
                One of 'gauss' and that's it.

    Methods
    -------
    run()
        Run the simulation, writing the data to disk.
    """
    def __init__(self, *,
                 seed, output_meds_dir, tilename, bands,
                 gal_kws, psf_kws):
        self.output_meds_dir = output_meds_dir
        self.tilename = tilename
        self.bands = bands
        self.gal_kws = gal_kws
        self.psf_kws = psf_kws
        self.seed = seed
        # any object within a 128 coadd pixel buffer of the edge of a CCD
        # will be rendered for that CCD
        self.bounds_buffer_uv = 128 * 0.263
        
        if self.psf_kws['type'] == 'psfex':
            self.draw_method = 'no_pixel'
        else:
            self.draw_method = 'auto'

        # make the RNGS. Extra initial seeds in case we need even more multiple random generators in future
        seeds = np.random.RandomState(seed=seed).randint(low=1, high=2**30, size=10)
        
        # one for galaxies (dither) in the truth catalog
        # one for noise in the images
        self.galdither_rng = np.random.RandomState(seed=seeds[0])
        self.noise_rng = np.random.RandomState(seed=seeds[1])
        
        #one for drawing random galaxies from descwl package
        self.galsource_rng = np.random.RandomState(seed=seeds[2])
        
        # load the image info for each band
        self.info = {}
        for band in bands:
            fname = get_band_info_file(
                meds_dir=self.output_meds_dir,
                medsconf=MEDSCONF,
                tilename=self.tilename,
                band=band)
            with open(fname, 'r') as fp:
                self.info[band] = yaml.load(fp, Loader=yaml.Loader)
        
    def run(self):
        """Run the simulation w/ galsim, writing the data to disk."""

        logger.info(' simulating coadd tile %s', self.tilename)

        # step 1 - Load simulated galaxy catalog if needed
        sim_cat   = self._make_sim_catalog() #Store catalog, but we don't really use it for anything
        
        # step 2 - make the truth catalog
        truth_cat = self._make_truth_catalog()
        

        # step 3 - per band, write the images to a tile
        for band in self.bands:
            self._run_band(band=band, truth_cat=truth_cat)

    def _run_band(self, *, band, truth_cat):
        """Run a simulation of a truth cat for a given band."""

        logger.info(" rendering images in band %s", band)

        noise_seeds = self.noise_rng.randint(
            low=1, high=2**30, size=len(self.info[band]['src_info']))
        
        jobs = []
        for noise_seed, se_info in zip(
                noise_seeds, self.info[band]['src_info']):

            src_func = LazySourceCat(
                truth_cat=truth_cat,
                wcs=get_galsim_wcs(
                    image_path=se_info['image_path'],
                    image_ext=se_info['image_ext']),
                psf=self._make_psf_wrapper(se_info=se_info),
                g1=self.gal_kws['g1'],
                g2=self.gal_kws['g2'],
                gal_mag = self.gal_kws['gal_mag'],
                gal_source = self.gal_kws['gal_source'],
                galsource_rng = self.galsource_rng,
                simulated_catalog = self.simulated_catalog)

            jobs.append(joblib.delayed(_render_se_image)(
                se_info=se_info,
                band=band,
                truth_cat=truth_cat,
                bounds_buffer_uv=self.bounds_buffer_uv,
                draw_method=self.draw_method,
                noise_seed=noise_seed,
                output_meds_dir=self.output_meds_dir,
                src_func=src_func,
                gal_kws = self.gal_kws))

        with joblib.Parallel(
                n_jobs=-1, backend='loky', verbose=50, max_nbytes=None) as p:
            p(jobs)

    def _make_psf_wrapper(self, *, se_info):
        
        wcs = get_galsim_wcs(image_path=se_info['image_path'], image_ext=se_info['image_ext'])

        if self.psf_kws['type'] == 'gauss':
            psf_model = galsim.Gaussian(fwhm=0.9)
        
        #elif self.psf_kws['type'] == 'piff':
        #    from ..des_piff import DES_Piff
        #    psf_model = DES_Piff(expand_path(se_info['piff_path']))
        #    assert self.draw_method == 'auto'
        
        elif self.psf_kws['type'] == 'gauss-pix':
            from gauss_pix_psf import GaussPixPSF
            kwargs = {k: self.psf_kws[k] for k in self.psf_kws if k != 'type'}
            psf_model = GaussPixPSF(**kwargs)
            assert self.draw_method == 'auto'
        
        elif self.psf_kws['type'] == 'nongauss-pix':
            from nongauss_pix_psf import NonGaussPixPSF
            kwargs = {k: self.psf_kws[k] for k in self.psf_kws if k != 'type'}
            psf_model = NonGaussPixPSF(**kwargs)
            assert self.draw_method == 'auto'

        elif self.psf_kws['type'] == 'psfex':
            from galsim.des import DES_PSFEx
            psf_model = DES_PSFEx(expand_path(se_info['psfex_path']), wcs = wcs) #Need to pass wcs when reading file
            assert self.draw_method == 'no_pixel'
        
        elif self.psf_kws['type'] == 'des_psfex':
            from des_psfex import DES_PSFEx_Deconv
            psf_model = DES_PSFEx_Deconv(expand_path(se_info['psfex_path']), wcs = wcs) #Need to pass wcs when reading file
            assert self.draw_method == 'auto' #Don't need no_pixel since psf already deconvolved
            
        elif self.psf_kws['type'] == 'psfex_deconvolved':
            from psfex_deconvolved import PSFEx_Deconv
            psf_model = PSFEx_Deconv(expand_path(se_info['psfex_path']), wcs = wcs) #Need to pass wcs when reading file
            assert self.draw_method == 'auto' #Don't need no_pixel since psf already deconvolved
        
        else:
            raise ValueError(
                "psf type '%s' not recognized!" % self.psf_kws['type'])

        psf_wrap = PSFWrapper(psf_model, wcs)

        return psf_wrap

    def _make_truth_catalog(self):
        """Make the truth catalog."""
        # always done with first band
        band = self.bands[0]
        coadd_wcs = get_esutil_wcs(
            image_path=self.info[band]['image_path'],
            image_ext=self.info[band]['image_ext'])

        ra, dec, x, y = make_coadd_grid_radec(
            rng=self.galdither_rng, coadd_wcs=coadd_wcs,
            return_xy=True, n_grid=self.gal_kws['n_grid'])

        truth_cat = np.zeros(len(ra), dtype=[('number', 'i8'), ('ind', 'i8'), 
                                             ('ra',  'f8'), ('dec', 'f8'), 
                                             ('x', 'f8'), ('y', 'f8'),
                                             ('a_world', 'f8'), ('b_world', 'f8'), ('size', 'f8')])
        truth_cat['number'] = np.arange(len(ra)).astype(np.int64) + 1
        truth_cat['ra']  = ra
        truth_cat['dec'] = dec
        truth_cat['x'] = x
        truth_cat['y'] = y
        
        if self.gal_kws['gal_source'] in ['varsize', 'varang', 'varsizeang']:
            truth_cat['ind']  = self.galsource_rng.randint(low=0, high=len(ra), size=len(ra))
            truth_cat['size'] = self.simulated_catalog['size'][truth_cat['ind']] #r = sqrt(a*b), q = b/a
            truth_cat['a_world'] = truth_cat['size']/np.sqrt(self.simulated_catalog['q'][truth_cat['ind']]) # a = r/sqrt(q)
            truth_cat['b_world'] = truth_cat['size']*np.sqrt(self.simulated_catalog['q'][truth_cat['ind']]) # b = r*sqrt(q)
            
        elif self.gal_kws['gal_source'] == 'descwl':
            truth_cat['ind'] = self.galsource_rng.randint(low=0, high=300_000, size=len(ra))
            truth_cat['a_world'] = self.simulated_catalog['a_d'][truth_cat['ind']]
            truth_cat['b_world'] = self.simulated_catalog['b_d'][truth_cat['ind']]
            truth_cat['size']    = np.sqrt(truth_cat['a_world']*truth_cat['b_world'])
            

        truth_cat_path = get_truth_catalog_path(
            meds_dir=self.output_meds_dir,
            medsconf=MEDSCONF,
            tilename=self.tilename)

        make_dirs_for_file(truth_cat_path)
        fitsio.write(truth_cat_path, truth_cat, clobber=True)

        return truth_cat

    def _make_sim_catalog(self):
        
        """Makes sim catalog"""
        
        if self.gal_kws['gal_source'] == 'simple':
            self.simulated_catalog = None
        
        #Same catalog generation if we want to vary size or angle
        
        elif self.gal_kws['gal_source'] in ['varsize', 'varang', 'varsizeang']:
            
            #Simulate 500,000 objects. We won't use that many per tile.
            #Hardcoding number because this happens before truth cat generation
            #so we dont know how many objects are in this coadd
            cat = np.zeros(500_000, dtype=[('size', 'f8'), ('q', 'f8'), ('ang_rot', 'f8')])
            
            cat['size']    = self.galsource_rng.uniform(self.gal_kws['size_min'], self.gal_kws['size_max'], len(cat)) #in arcsec
            cat['q']       = self.galsource_rng.uniform(self.gal_kws['q_min'],    self.gal_kws['q_max'],    len(cat)) #dimensionless
            cat['ang_rot'] = self.galsource_rng.uniform(0, 360, len(cat)) #in degrees

            self.simulated_catalog = cat
            
        elif self.gal_kws['gal_source'] == 'descwl':
            self.simulated_catalog = init_descwl_catalog(survey_bands = "des-riz", rng = self.galsource_rng)

            #Temporarily remove all ellipticity
            self.simulated_catalog.cat['a_d'] = self.simulated_catalog.cat['a_d']
            self.simulated_catalog.cat['b_d'] = self.simulated_catalog.cat['a_d']
            self.simulated_catalog.cat['a_b'] = self.simulated_catalog.cat['a_b']
            self.simulated_catalog.cat['b_b'] = self.simulated_catalog.cat['a_b']
            
            #temporarily induce ellipticity
#             self.simulated_catalog.cat['a_d'] = self.simulated_catalog.cat['a_d']
#             self.simulated_catalog.cat['b_d'] = self.simulated_catalog.cat['a_d']*0.2
#             self.simulated_catalog.cat['a_b'] = self.simulated_catalog.cat['a_b']
#             self.simulated_catalog.cat['b_b'] = self.simulated_catalog.cat['a_b']*0.2
            
            #Temporarily set to zero just to ensure all galaxies have same direction
#             simulated_catalog.cat['pa_disk'][:] = 0
#             simulated_catalog.cat['pa_bulge'][:] = 0
#             simulated_catalog.cat['a_d'] = self.galsource_rng.uniform(0.4, 0.8, len(simulated_catalog.cat['a_d'])) #0.2
#             simulated_catalog.cat['b_d'] = simulated_catalog.cat['a_d'] #0.2
#             simulated_catalog.cat['a_b'] = 0.05
#             simulated_catalog.cat['b_b'] = 0.05

        return self.simulated_catalog

def _render_se_image(
        *, se_info, band, truth_cat, bounds_buffer_uv,
        draw_method, noise_seed, output_meds_dir, src_func, gal_kws):
    """Render an SE image.

    This function renders a full image and writes it to disk.

    Parameters
    ----------
    se_info : dict
        The entry from the `src_info` list for the coadd tile.
    band : str
        The band as a string.
    truth_cat : np.ndarray
        A structured array with the truth catalog. Must at least have the
        columns 'ra' and 'dec' in degrees.
    bounds_buffer_uv : float
        The buffer in arcseconds for finding sources in the image. Any source
        whose center lies outside of this buffer area around the CCD will not
        be rendered for that CCD.
    draw_method : str
        The method used to draw the image. See the docs of `GSObject.drawImage`
        for details and options. Usually 'auto' is correct unless using a
        PSF with the pixel in which case 'no_pixel' is the right choice.
    noise_seed : int
        The RNG seed to use to generate the noise field for the image.
    output_meds_dir : str
        The output DEADATA/MEDS_DIR for the simulation data products.
    src_func : callable
        A function with signature `src_func(src_ind)` that
        returns the galsim object to be rendered and image position
        for a given index of the truth catalog.
    gal_kws : dict
        Dictionary containing the keywords passed to the
        the simulating code
    """

    # step 1 - get the set of good objects for the CCD
    msk_inds = _cut_tuth_cat_to_se_image(
        truth_cat=truth_cat,
        se_info=se_info,
        bounds_buffer_uv=bounds_buffer_uv)

    # step 2 - render the objects
    im = _render_all_objects(
        msk_inds=msk_inds,
        truth_cat=truth_cat,
        se_info=se_info,
        band=band,
        src_func=src_func,
        draw_method=draw_method)

    # step 3 - add bkg and noise
    # also removes the zero point
    im, wgt, bkg, bmask = _add_noise_mask_background(
        image=im,
        se_info=se_info,
        noise_seed=noise_seed,
        gal_kws = gal_kws)

    # step 4 - write to disk
    _write_se_img_wgt_bkg(
        image=im,
        weight=wgt,
        background=bkg,
        bmask=bmask,
        se_info=se_info,
        output_meds_dir=output_meds_dir)


def _cut_tuth_cat_to_se_image(*, truth_cat, se_info, bounds_buffer_uv):
    """get the inds of the objects to render from the truth catalog"""
    wcs = get_esutil_wcs(
        image_path=se_info['image_path'],
        image_ext=se_info['image_ext'])
    sky_bnds, ra_ccd, dec_ccd = get_rough_sky_bounds(
        im_shape=se_info['image_shape'],
        wcs=wcs,
        position_offset=se_info['position_offset'],
        bounds_buffer_uv=bounds_buffer_uv,
        n_grid=4)
    u, v = radec_to_uv(truth_cat['ra'], truth_cat['dec'], ra_ccd, dec_ccd)
    sim_msk = sky_bnds.contains_points(u, v)
    msk_inds, = np.where(sim_msk)
    return msk_inds


def _render_all_objects(
        *, msk_inds, truth_cat, se_info, band, src_func, draw_method):
    gs_wcs = get_galsim_wcs(
        image_path=se_info['image_path'],
        image_ext=se_info['image_ext'])

    im = render_sources_for_image(
        image_shape=se_info['image_shape'],
        wcs=gs_wcs,
        draw_method=draw_method,
        src_inds=msk_inds,
        src_func=src_func,
        n_jobs=1)

    return im.array


def _add_noise_mask_background(*, image, se_info, noise_seed, gal_kws):
    """add noise, mask and background to an image, remove the zero point"""

    noise_rng = np.random.RandomState(seed=noise_seed)

    # first back to ADU units
    image /= se_info['scale']

    # add the background
    bkg = fitsio.read(se_info['bkg_path'], ext=se_info['bkg_ext'])
    image += bkg

    # now add noise
    wgt = fitsio.read(se_info['weight_path'], ext=se_info['weight_ext'])
    bmask = fitsio.read(se_info['bmask_path'], ext=se_info['bmask_ext'])
    img_std = 1.0 / np.sqrt(np.median(wgt[bmask == 0]))
    image += (noise_rng.normal(size=image.shape) * img_std)
    wgt[:, :] = 1.0 / img_std**2
    
    
    if gal_kws['Mask'] == True:
        
        pass
        #mask the image
#         image[bmask.astype(bool)] = np.NaN
#         wgt[bmask.astype(bool)]   = np.NaN
        
    elif gal_kws['Mask'] == False:
        bmask = np.zeros_like(bmask)
        
    else:
        raise ValueError("Unknown value %s for keyword {Mask}. Choose True or False"%str(self.gal_kws['Mask']))
    
#     bmask = np.zeros_like(bmask)
        

    return image, wgt, bkg, bmask


def _write_se_img_wgt_bkg(
        *, image, weight, background, bmask, se_info, output_meds_dir):
    # these should be the same
    assert se_info['image_path'] == se_info['weight_path'], se_info
    assert se_info['image_path'] == se_info['bmask_path'], se_info

    # and not this
    assert se_info['image_path'] != se_info['bkg_path']

    # get the final image file path and write
    image_file = se_info['image_path'].replace(
        TMP_DIR, output_meds_dir)
    make_dirs_for_file(image_file)
    with tempfile.TemporaryDirectory() as tmpdir:
        with StagedOutFile(image_file, tmpdir=tmpdir) as sf:
            # copy to the place we stage from
            shutil.copy(expand_path(se_info['image_path']), sf.path)

            # open in read-write mode and replace the data
            with fitsio.FITS(sf.path, mode='rw') as fits:
                fits[se_info['image_ext']].write(image)
                fits[se_info['weight_ext']].write(weight)
#                 fits[se_info['bmask_ext']].write(np.zeros_like(image, dtype=np.int16))
                fits[se_info['bmask_ext']].write(bmask)

    # get the background file path and write
    bkg_file = se_info['bkg_path'].replace(
        TMP_DIR, output_meds_dir)
    make_dirs_for_file(bkg_file)
    with tempfile.TemporaryDirectory() as tmpdir:
        with StagedOutFile(bkg_file, tmpdir=tmpdir) as sf:
            # copy to the place we stage from
            shutil.copy(expand_path(se_info['bkg_path']), sf.path)

            # open in read-write mode and replace the data
            with fitsio.FITS(sf.path, mode='rw') as fits:
                fits[se_info['bkg_ext']].write(background)


class LazySourceCat(object):
    """A lazy source catalog that only builds objects to be rendered as they
    are needed.

    Parameters
    ----------
    truth_cat : structured np.array
        The truth catalog as a structured numpy array.
    wcs : galsim.GSFitsWCS
        A galsim WCS instance for the image to be rendered.
    psf : PSFWrapper
        A PSF wrapper object to use for the PSF.
    g1 : float
        The shear to apply on the 1-axis.
    g2 : float
        The shear to apply on the 2-axis.

    Methods
    -------
    __call__(ind)
        Returns the object to be rendered from the truth catalog at
        index `ind`.
    """
    def __init__(self, *, truth_cat, wcs, psf, g1, g2, gal_mag, gal_source, galsource_rng = None, simulated_catalog = None):
        self.truth_cat = truth_cat
        self.wcs = wcs
        self.psf = psf
        self.g1 = g1
        self.g2 = g2
        
        self.gal_source = gal_source
        self.galsource_rng = galsource_rng
        
        self.simulated_catalog = simulated_catalog
        
        self.gal_mag = gal_mag
        
            

    def __call__(self, ind):
        pos = self.wcs.toImage(galsim.CelestialCoord(
            ra=self.truth_cat['ra'][ind] * galsim.degrees,
            dec=self.truth_cat['dec'][ind] * galsim.degrees))
        
        
        if self.gal_source == 'simple':
            obj = galsim.Exponential(half_light_radius=0.5)
            
        elif self.gal_source == 'varsize':
            rad = self.simulated_catalog['size'][self.truth_cat['ind'][ind]] #Get radius from catalog (in arcmin)
            
            obj = galsim.Exponential(half_light_radius=rad)
            
        elif self.gal_source == 'varang':
            q   = self.simulated_catalog['q'][self.truth_cat['ind'][ind]] #Get ellipticity
            rot = self.simulated_catalog['ang_rot'][self.truth_cat['ind'][ind]] #Get rotation of galaxy
            
            obj = galsim.Exponential(half_light_radius=0.5).shear(q = q, beta = rot * galsim.degrees)
            
        elif self.gal_source == 'varsizeang':
            rad = self.simulated_catalog['size'][self.truth_cat['ind'][ind]] #Get radius from catalog (in arcmin)
            q   = self.simulated_catalog['q'][self.truth_cat['ind'][ind]] #Get ellipticity
            rot = self.simulated_catalog['ang_rot'][self.truth_cat['ind'][ind]] #Get rotation of galaxy
            
            #Take exponential profile, shear it to cause intrinsic ellipticity in direction given by rot
            obj = galsim.Exponential(half_light_radius=rad).shear(q = q, beta = rot * galsim.degrees)
            
        elif self.gal_source == 'descwl':
            
            obj = get_descwl_galaxy(descwl_ind = self.truth_cat['ind'][ind],
                                    rng  = self.galsource_rng, 
                                    data = self.simulated_catalog)
            
        if self.gal_mag is not None:
            normalized_flux = 10**((30 - self.gal_mag)/2.5)
            obj = obj.withFlux(normalized_flux)
        
        obj = obj.shear(g1=self.g1, g2=self.g2)
        psf = self.psf.getPSF(image_pos=pos)
        
        return galsim.Convolve([obj, psf]), pos