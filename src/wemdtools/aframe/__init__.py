"""WEMD Analyis framework"""

from __future__ import division, print_function; __metaclass__ = type

class ArgumentError(RuntimeError):
    def __init__(self, *args, **kwargs):
        super(ArgumentError,self).__init__(*args,**kwargs)

class AnalysisMixin:
    def __init__(self):
        super(AnalysisMixin,self).__init__()
        
    def add_common_args(self, parser, upcall = True):
        if upcall:
            try:
                upfunc = super(AnalysisMixin,self).add_common_args
            except AttributeError:
                pass
            else:
                upfunc(parser)
    
    def process_common_args(self, args, upcall = True):
        if upcall:
            try:
                upfunc = super(AnalysisMixin,self).process_common_args
            except AttributeError:
                pass
            else:
                upfunc(args)
    
import atool
from atool import WEMDAnalysisTool
from default_mixins import IterRangeMixin
from data_reader import DataReaderMixin
from binning import BinningMixin
