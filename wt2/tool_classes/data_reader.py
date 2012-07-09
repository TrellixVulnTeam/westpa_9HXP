from __future__ import division, print_function; __metaclass__ = type
from core import WEMDTool
import os
import numpy, h5py
import wemd

class WEMDDataReader(WEMDTool):
    '''Tool for reading data from WEMD-related HDF5 files. Coordinates finding
    the main HDF5 file from wemd.cfg or command line arguments, caching of certain
    kinds of data (eventually), and retrieving auxiliary data sets from various
    places.'''
    
    def __init__(self):
        super(WEMDDataReader,self).__init__()
        self.data_manager = wemd.rc.get_data_manager() 
        self.we_h5filename = None
        
        self._h5files = {}

    def add_args(self, parser):
        group = parser.add_argument_group('WEMD input data options')
        group.add_argument('-W', '--wemd-data', dest='we_h5filename', metavar='WEMD_H5FILE',
                           help='''Take WEMD data from WEMD_H5FILE (default: read from the HDF5 file specified in wemd.cfg).''')
        
    def process_args(self, args):
        if args.we_h5filename:
            self.data_manager.we_h5filename = self.we_h5filename = args.we_h5filename
        else:
            self.we_h5filename = self.data_manager.we_h5filename
        
    def open(self, mode='r'):
        self.data_manager.open_backing(mode)
        
    def close(self):
        self.data_manager.close_backing()
        
    def __getattr__(self, key):
        return getattr(self.data_manager, key)

    def parse_dssel_str(self, dsstr):
        '''Parse a data set specification field, as in::
        
            dsname;alias=newname;index=idsname,;ile=otherfile.h5;slice=[100,...]
            
        Returns a dictionary containing option->value mappings.
            
        The following options for datasets are supported:
        
            alias=newname
                When writing this data to HDF5 or text files, use ``newname``
                instead of ``dsname`` to identify the dataset. This is mostly of
                use in conjunction with the ``slice`` option in order, e.g., to
                retrieve two different slices of a dataset and store them with
                different names for future use.
        
            index=idsname
                The dataset is not stored on a per-iteration basis for all
                segments, but instead is stored as a single dataset whose
                first dimension indexes n_iter/seg_id pairs. The index to
                these n_iter/seg_id pairs is ``idsname``. 
            
            file=otherfile.h5
                Instead of reading data from the main WEMD HDF5 file (usually
                ``wemd.h5``), read data from ``otherfile.h5``.
                
            slice=[100,...]
                Retrieve only the given slice from the dataset. This can be
                used to pick a subset of interest to minimize I/O. Stored as
                a slice object as generated by ``numpy.index_exp``.
                
        '''
        
        fields = dsstr.split(';')
        
        dsname = fields[0]
        filename = None
        alias = None
        index = None
        sl = None
        
        for field in (field.strip() for field in fields[1:]):
            k,v = field.split('=')
            k = k.lower()
            if not k:
                continue
            elif k == 'alias':
                alias = v
            elif k == 'index':
                index = v
            elif k == 'file':
                filename = v
            elif k == 'slice':
                try:
                    sl = eval('numpy.index_exp' + v)
                except SyntaxError:
                    raise SyntaxError('invalid index expression {!r}'.format(v))
            else:
                raise ValueError('invalid dataset option {!r}'.format(k))
        
        if not filename:
            h5file = self.data_manager.we_h5file
        else:
            filename = os.path.abspath(filename)
            try:
                h5file = self._h5files[filename]
            except KeyError:
                h5file = self._h5files[filename] = h5py.File(filename, 'r')
        
        return ByIterDataSelection(h5file, dsname, sl, alias, index)


class DataSelection:
    def __init__(self, h5file, dataset, slice = None, alias = None):
        self.h5file = h5file
        self.source_dsname = dataset
        self.slice = slice or numpy.index_exp[...]
        self.alias = alias
            
    @property
    def name(self):
        return self.alias or self.source_dsname
    
    @name.setter
    def name(self, alias):
        self.alias = alias

class ByIterDataSelection(DataSelection):
    def __init__(self, h5file, dataset, slice = None, alias = None, index=None):
        super(ByIterDataSelection,self).__init__(h5file, dataset, slice, alias)
        self.index = index
        self._index_data = None
        self._iter_prec = None
        
        self._iter_groups = {}
        
    def _get_iter_group(self, n_iter):
        try:
            return self._iter_groups[n_iter]
        except KeyError:
            if self._iter_prec is None:
                self._iter_prec = self.h5file['/'].attrs.get('wemd_iter_prec',8)
            
            try:
                iter_group = self.h5file['/iterations/iter_{:0{prec}d}'.format(n_iter,prec=self._iter_prec,)]
            except KeyError:
                try:
                    iter_group = self.h5file['/iter_{:0{prec}d}'.format(n_iter,prec=self._iter_prec)]
                except KeyError:
                    raise KeyError('iteration {:d} not found'.format(n_iter))
            
            self._iter_groups[n_iter] = iter_group
            return iter_group        
        
    def __getitem__(self, pair):
        '''Retrieve data for the given iteration or (n_iter,seg_id) pair from datasets split by iteration.'''
        
        try:
            (n_iter, seg_id) = pair
        except TypeError:
            n_iter = pair
            seg_id = None
        except ValueError:
            n_iter = pair[0]
            seg_id = None
        
        if seg_id is None:
            if self.index:
                return self._getiter_indexed(n_iter)
            else:
                return self._getiter_unindexed(n_iter)
        else:    
            if self.index:
                return self._getseg_indexed(n_iter,seg_id)
            else:
                return self._getseg_unindexed(n_iter, seg_id)
            
            
    def _getseg_indexed(self, n_iter, seg_id):
        if self._index_data is None:
            self._index_data = {(int(_iter), long(_seg)): i for (i, (_iter, _seg)) in enumerate(self.h5file[self.index][...])}
            
        i = self._index_data[int(n_iter), long(seg_id)]
        itpl = (i,) + self.slice
        return self.h5file[self.source_dsname][itpl]
    
    def _getiter_indexed(self, n_iter):
        if self._index_data is None:
            self._index_data = {(int(_iter), long(_seg)): i for (i, (_iter, _seg)) in enumerate(self.h5file[self.index][...])}
            
        segspecs = [(_n_iter, seg_id) for (_n_iter,seg_id) in self._index_data.iterkeys() if _n_iter==n_iter]
        indices = []
        for (n_iter, seg_id) in segspecs:
            indices.append(self._index_data[int(n_iter), long(seg_id)])
        indices.sort()
        itpl = (indices,) + self.slice
        return self.h5file[self.source_dsname][itpl]
        
    def _getseg_unindexed(self, n_iter, seg_id):
        itpl = (seg_id,) + self.slice
        iter_group = self._get_iter_group(n_iter)
        return iter_group[self.source_dsname][itpl]
    
    def _getiter_unindexed(self, n_iter):
        itpl = numpy.index_exp[:] + self.slice
        iter_group = self._get_iter_group(n_iter)
        return iter_group[self.source_dsname][itpl]
        
    @property
    def indexed(self):
        return bool(self.index)
    
    
    

