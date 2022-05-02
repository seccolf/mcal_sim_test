import logging
import numpy as np

import joblib
import esutil as eu
import fitsio
from ngmix import ObsList, MultiBandObsList
from ngmix.gexceptions import GMixRangeError
#from ngmix_compat import NGMixMEDS, MultiBandNGMixMEDS, NGMIX_V1
from ngmix.medsreaders import NGMixMEDS, MultiBandNGMixMEDS#, NGMIX_V1
NGMIX_V1=False #wild guess here

from files import get_meds_file_path, get_mcal_file_path, make_dirs_for_file
from metacal.metacal_fitter import MetacalFitter
from constants import MEDSCONF, MAGZP_REF
from interpolate import interpolate_image_at_mask
import galsim

logger = logging.getLogger(__name__)

CONFIG = {
    'metacal': {
        # check for an edge hit
        'bmask_flags': 2**30,

        'model': 'gauss',

        'max_pars': {
            'ntry': 2,
            'pars': {
                'method': 'lm',
                'lm_pars': {
                    'maxfev': 2000,
                    'xtol': 5.0e-5,
                    'ftol': 5.0e-5,
                }
            }
        },

        'priors': {
            'cen': {
                'type': 'normal2d',
                'sigma': 0.263
            },

            'g': {
                'type': 'ba',
                'sigma': 0.2
            },

            'T': {
                'type': 'two-sided-erf',
                'pars': [-1.0, 0.1, 1.0e+06, 1.0e+05]
            },

            'flux': {
                'type': 'two-sided-erf',
                'pars': [-100.0, 1.0, 1.0e+09, 1.0e+08]
            }
        },

        'psf': {
            'model': 'gauss',
            'ntry': 2,
            'lm_pars': {
                'maxfev': 2000,
                'ftol': 1.0e-5,
                'xtol': 1.0e-5
            }
        }
    },
}

if NGMIX_V1:
    CONFIG['metacal']['metacal_pars'] = {
        'types': ['noshear', '1p', '1m', '2p', '2m'],
        # 'symmetrize_psf': True
    }
else:
    CONFIG['metacal']['metacal_pars'] = {
        'psf': 'fitgauss',
        'types': ['noshear', '1p', '1m', '2p', '2m'],
        # 'use_noise_image': True,
    }


def run_metacal(*, tilename, output_meds_dir, bands, seed):
    """Run metacal on a tile.

    Parameters
    ----------
    tilename : str
        The DES coadd tile on which to run metacal.
    output_meds_dir : str
        The output DEADATA/MEDS_DIR for the simulation data products.
    bands : str
        The bands on which to run metacal.
    seed : int
        The seed for the global RNG.
    """
    meds_files = [
        get_meds_file_path(
            meds_dir=output_meds_dir,
            medsconf=MEDSCONF,
            tilename=tilename,
            band=band)
        for band in bands]
    with NGMixMEDS(meds_files[0]) as m:
        cat = m.get_cat()
    logger.info(' meds files %s', meds_files)

    n_chunks = joblib.externals.loky.cpu_count()
    n_obj_per_chunk = cat.size // n_chunks
    if n_obj_per_chunk * n_chunks < cat.size:
        n_obj_per_chunk += 1
    assert n_obj_per_chunk * n_chunks >= cat.size
    logger.info(
        ' running metacal for %d objects in %d chunks', cat.size, n_chunks)

    seeds = np.random.RandomState(seed=seed).randint(1, 2**30, size=n_chunks)

    jobs = []
    for chunk in range(n_chunks):
        start = chunk * n_obj_per_chunk
        end = min(start + n_obj_per_chunk, cat.size)
        jobs.append(joblib.delayed(_run_mcal_one_chunk)(
            meds_files, start, end, seeds[chunk]))

    with joblib.Parallel(
            n_jobs=n_chunks, backend='loky',
            verbose=50, max_nbytes=None) as p:
        outputs = p(jobs)

    assert not all([o is None for o in outputs]), (
        "All metacal fits failed!")

    output = eu.numpy_util.combine_arrlist(
        [o for o in outputs if o is not None])
    logger.info(' %d of %d metacal fits worked!', output.size, cat.size)

    mcal_pth = get_mcal_file_path(
        meds_dir=output_meds_dir,
        medsconf=MEDSCONF,
        tilename=tilename)
    logger.info(' metacal output: "%s"', mcal_pth)
    make_dirs_for_file(mcal_pth)
    fitsio.write(mcal_pth, output, clobber=True)


def _run_mcal_one_chunk(meds_files, start, end, seed):
    """Run metcal for `meds_files` only for objects from `start` to `end`.

    Note that `start` and `end` follow normal python indexing conventions so
    that the list of indices processed is `list(range(start, end))`.

    Parameters
    ----------
    meds_files : list of str
        A list of paths to the MEDS files.
    start : int
        The starting index of objects in the file on which to run metacal.
    end : int
        One plus the last index to process.
    seed : int
        The seed for the RNG.

    Returns
    -------
    output : np.ndarray
        The metacal outputs.
    """
    rng = np.random.RandomState(seed=seed)

    # seed the global RNG to try to make things reproducible
    np.random.seed(seed=rng.randint(low=1, high=2**30))

    output = None
    mfiles = []
    data = []
    try:
        # get the MEDS interface
        for m in meds_files:
            mfiles.append(NGMixMEDS(m))
        mbmeds = MultiBandNGMixMEDS(mfiles)
        cat = mfiles[0].get_cat()

        for ind in range(start, end):
            o = mbmeds.get_mbobs(ind)
            
            o = _strip_coadd(o) #Remove coadd since it isnt used in fitting
            o = _strip_zero_flux(o) #Remove any obs with zero flux
            o = _fill_empty_pix(o) #Interpolate empty pixels (and remove img where we cant do it properly)
            o = _apply_pixel_scale(o) #Get right flux scale

            skip_me = False
            for ol in o:
                if len(ol) == 0:
                    logger.debug(' not all bands have images - skipping!')
                    skip_me = True
            if skip_me:
                continue

            o.meta['id'] = ind
            o[0].meta['Tsky'] = 1
            o[0].meta['magzp_ref'] = MAGZP_REF
            o[0][0].meta['orig_col'] = cat['orig_col'][ind, 0]
            o[0][0].meta['orig_row'] = cat['orig_row'][ind, 0]

            nband = len(o)
            mcal = MetacalFitter(CONFIG, nband, rng)

            try:
                mcal.go([o])
                res = mcal.result
            except GMixRangeError as e:
                logger.debug(" metacal error: %s", str(e))
                res = None

            if res is not None:
                data.append(res)

        if len(data) > 0:
            output = eu.numpy_util.combine_arrlist(data)
    finally:
        for m in mfiles:
            m.close()

    return output


def _strip_coadd(mbobs):
    _mbobs = MultiBandObsList()
    _mbobs.update_meta_data(mbobs.meta)
    for ol in mbobs:
        _ol = ObsList()
        _ol.update_meta_data(ol.meta)
        for i in range(1, len(ol)):
            _ol.append(ol[i])
        _mbobs.append(_ol)
    return _mbobs


def _strip_zero_flux(mbobs):
    _mbobs = MultiBandObsList()
    _mbobs.update_meta_data(mbobs.meta)
    for ol in mbobs:
        _ol = ObsList()
        _ol.update_meta_data(ol.meta)
        for i in range(len(ol)):
            if np.sum(ol[i].image) > 0:
                _ol.append(ol[i])
        _mbobs.append(_ol)
    return _mbobs


def _apply_pixel_scale(mbobs):
    for ol in mbobs:
        for o in ol:
            scale = o.jacobian.get_scale()
            scale2 = scale * scale
            scale4 = scale2 * scale2
            o.image = o.image / scale2
            o.weight = o.weight * scale4
    return mbobs


def _fill_empty_pix(mbobs):
    _mbobs = MultiBandObsList()
    _mbobs.update_meta_data(mbobs.meta)
    
    gauss = galsim.Gaussian(fwhm = 1.2) #Fixed aperture gauss weights for image
    
    #Loop over different band observations (r, i, z)
    for ol in mbobs:
        _ol = ObsList()
        _ol.update_meta_data(ol.meta)
        
        #Loop over different exposures/cutouts in each band
        for i in range(len(ol)):
            
            msk = ol[i].bmask.astype(bool) #Mask where TRUE means bad pixel
            wgt = np.median(ol[i].weight) #Median weight used to populate noise in empty pix
            
            wcs       = ol[i].jacobian.get_galsim_wcs() #get wcs of this observations
            gauss_wgt = gauss.drawImage(nx = msk.shape[0], ny = msk.shape[1], wcs = wcs).array #Create gaussian weights image (as array)
            
            good_frac = np.average(np.invert(msk).astype(int), weights = gauss_wgt) #Fraction of missing values
            
            #if weighted frac of good pixs is low, then skip observation
            if good_frac < 0.9:
                continue
            
            #Interpolate image to fill in gaps
            im    = interpolate_image_at_mask(image=ol[i].image, weight=wgt, bad_msk=msk, 
                                              rng=rng, maxfrac=0.9, buff=4,
                                              fill_isolated_with_noise=True)
                                                       
            noise = interpolate_image_at_mask(image=ol[i].noise, weight=wgt, bad_msk=msk, 
                                              rng=rng, maxfrac=0.9, buff=4,
                                              fill_isolated_with_noise=True)
            
            ol[i].image = im
            ol[i].noise = noise
            
            #If we can't interpolate image or noise due to lack of data
            #then we skip this observation (it is stripped from MultiBandObs list)
            if (im is None) | (noise is None):
                continue
            
            _ol.append(ol[i])
        _mbobs.append(_ol)
    return _mbobs