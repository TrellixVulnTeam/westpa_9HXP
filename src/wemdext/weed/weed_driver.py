from __future__ import division; __metaclass__ = type

import logging
log = logging.getLogger(__name__)

import numpy
from itertools import izip

from wemd.util.miscfn import vgetattr
import wemd
from wemdext.weed.ProbAdjustEquil import probAdjustEquil

class WEEDDriver:
    def __init__(self, sim_manager):
        if sim_manager.work_manager.mode != sim_manager.work_manager.MODE_MASTER: 
            return

        self.sim_manager = sim_manager
        self.data_manager = sim_manager.data_manager
        self.system = sim_manager.system

        self.do_reweight = wemd.rc.config.get_bool('weed.do_equilibrium_reweighting', False)
        self.windowsize = 0.5
        self.windowtype = 'fraction'
        self.recalc_rates = wemd.rc.config.get_bool('weed.recalc_rates', False)
        
        windowsize = wemd.rc.config.get('weed.window_size', None)

        self.max_windowsize = wemd.rc.config.get_int('weed.max_window_size', None)
        if self.max_windowsize is not None:
            log.info('Using max windowsize of {:d}'.format(self.max_windowsize))

        if windowsize is not None:
            if '.' in windowsize:
                self.windowsize = float(windowsize)
                self.windowtype = 'fraction'
                if self.windowsize <= 0 or self.windowsize > 1:
                    raise ValueError('WEED parameter error -- fractional window size must be in (0,1]')
                log.info('using fractional window size of {:g}*n_iter'.format(self.windowsize))
            else:
                self.windowsize = int(windowsize)
                self.windowtype = 'fixed'
                log.info('using fixed window size of {:d}'.format(self.windowsize))
        
        self.reweight_period = wemd.rc.config.get_int('weed.reweight_period', 0)
        
        self.priority = wemd.rc.config.get_int('weed.priority',0)
        
        if sim_manager.target_states and self.do_reweight:
            log.warning('equilibrium reweighting requested but target states (sinks) present; reweighting disabled')
            self.do_reweight = False 
        else:
            sim_manager.register_callback(sim_manager.prepare_new_segments, self.prepare_new_segments, self.priority)    

    def calculate_rates(self,iter_group):
        '''Calculate instantaneous rate based on current bin definitions for iter_group'''
        
        bins = self.system.curr_region_set.get_all_bins()
        n_bins = len(bins)
        
        rates = numpy.zeros((n_bins,n_bins), numpy.float64)

        with self.data_manager.lock:
            pcoords = iter_group['pcoord']
            assignments = numpy.empty((pcoords.shape[0], 2), numpy.int)
            weights = iter_group['seg_index']['weight']

            assignments[:,0] = self.system.curr_region_set.map_to_all_indices(pcoords[:,0,:]).astype(numpy.int)
            assignments[:,1] = self.system.curr_region_set.map_to_all_indices(pcoords[:,-1,:]).astype(numpy.int)

            populations = numpy.bincount(assignments[:,0], weights=weights, minlength=n_bins)
            flattened_assign_ids = numpy.ravel_multi_index((assignments[:,0],assignments[:,1]), rates.shape)

            fluxes = numpy.bincount(flattened_assign_ids, weights=weights, minlength=n_bins*n_bins)
            fluxes = fluxes.reshape((n_bins,n_bins))

        for i in xrange(0,n_bins):
            if populations[i] > 0:
                rates[i,:] = fluxes[i,:] / populations[i]

        return rates 

    def get_rates(self, n_iter, bins):
        '''Get rates and associated uncertainties as of n_iter, according to the window size the user
        has selected (self.windowsize)'''
        
        n_bins = len(bins)
        
        if self.windowtype == 'fraction':
            if self.max_windowsize is not None:
                eff_windowsize = min(self.max_windowsize,int(n_iter * self.windowsize))
            else:
                eff_windowsize = int(n_iter * self.windowsize)
                
        else: # self.windowtype == 'fixed':
            eff_windowsize = min(n_iter, self.windowsize or 0)

        rates = numpy.ma.masked_all((eff_windowsize,n_bins,n_bins), numpy.float64)

        n_used = 0
        for n in xrange(n_iter, max(n_iter-eff_windowsize,1), -1):
            log.debug('considering iteration {:d}'.format(n))
            with self.data_manager.lock:
                iter_group = self.data_manager.get_iter_group(n)

                if self.recalc_rates:
                    rates_ds = self.calculate_rates(iter_group)
                else:
                    rates_ds = iter_group['bin_rates']

                if rates_ds.shape != rates.shape[1:]:
                    # A bin topology change means we can't go any farther back
                    wemd.rc.pstatus(('Rate matrix for iteration {:d} is of the wrong shape; '
                                      +'stopping accumulation of average rate data.\n').format(n))
                    wemd.rc.pflush()
                    break

                # Mask rows where bin probability is zero
                mrates = numpy.ma.array(rates_ds[...])

                # Find bins with zero probability
                binprobs = iter_group['bin_populations'][:,0]
                zindx = numpy.where(binprobs == 0.0)[0]

                mrates[zindx,zindx] = numpy.ma.masked
                mrates = numpy.ma.mask_rows(mrates)

                rates[n_used] = mrates

                n_used += 1

        avg_rates = rates.mean(axis=0)
        if n_used == 1:
            unc_rates = avg_rates.copy()
        else:
            unc_rates = rates.std(axis=0) / numpy.sqrt(numpy.sum(~rates.mask,0))
        return (avg_rates.data, unc_rates.data, n_used)

    def prepare_new_segments(self):
        n_iter = self.sim_manager.n_iter
        
        if not self.do_reweight:
            # Reweighting not requested (or not possible)
            log.debug('equilibrium reweighting not enabled') 
            return

        # We already have initial and final binning information for the current iteration
        # and initial binning for the new iteration (in self.next_iter_binning); no need to bin again
        bins = self.sim_manager.next_iter_binning.get_all_bins()
        n_bins = len(bins)         

        with self.data_manager.lock:
            iter_group = self.data_manager.get_iter_group(n_iter)
            try:
                del iter_group['weed']
            except KeyError:
                pass
                
            weed_iter_group = iter_group.create_group('weed')
            avg_rates_ds = weed_iter_group.create_dataset('avg_rates', shape=(n_bins,n_bins), dtype=numpy.float64)
            unc_rates_ds = weed_iter_group.create_dataset('unc_rates', shape=(n_bins,n_bins), dtype=numpy.float64)
            weed_global_group = self.data_manager.we_h5file.require_group('weed')
            last_reweighting = long(weed_global_group.attrs.get('last_reweighting', 0))
        
        if n_iter - last_reweighting < self.reweight_period:
            # Not time to reweight yet
            log.debug('not reweighting')
            return
        else:
            log.debug('reweighting')
        
        with self.data_manager.lock:
            avg_rates, unc_rates, eff_windowsize = self.get_rates(n_iter, bins)
            avg_rates_ds[...] = avg_rates
            unc_rates_ds[...] = unc_rates
            
            binprobs = iter_group['bin_populations'][:,-1]
            assert numpy.allclose(binprobs, vgetattr('weight', bins, numpy.float64))
            orig_binprobs = binprobs.copy()
        
        wemd.rc.pstatus('Calculating equilibrium reweighting using window size of {:d}'.format(eff_windowsize))
        wemd.rc.pstatus('\nBin probabilities prior to reweighting:\n{!s}'.format(binprobs))
        wemd.rc.pflush()

        probAdjustEquil(binprobs, avg_rates, unc_rates)
        
        # Check to see if reweighting has set non-zero bins to zero probability (should never happen)
        assert (~((orig_binprobs > 0) & (binprobs == 0))).all(), 'populated bin reweighted to zero probability'
        
        # Check to see if reweighting has set zero bins to nonzero probability (may happen)
        z2nz_mask = (orig_binprobs == 0) & (binprobs > 0) 
        if (z2nz_mask).any():
            wemd.rc.pstatus('Reweighting would assign nonzero probability to an empty bin; not reweighting this iteration.')
            wemd.rc.pstatus('Empty bins assigned nonzero probability: {!s}.'
                                .format(numpy.array_str(numpy.arange(n_bins)[z2nz_mask])))
        else:
            wemd.rc.pstatus('\nBin populations after reweighting:\n{!s}'.format(binprobs))
            for (bin, newprob) in izip(bins, binprobs):
                bin.reweight(newprob)
            weed_global_group.attrs['last_reweighting'] = n_iter
            
        assert abs(1 - vgetattr('weight', self.sim_manager.next_iter_segments, numpy.float64).sum()) < 1.0e-15*len(self.sim_manager.next_iter_segments)
