#!/usr/bin/env python

from __future__ import print_function
import ncepbufr
import numpy as np
import sys
import os
import re
import argparse
import netCDF4
from netCDF4 import Dataset
import struct
import datetime as dt

###########################################################################
# CONFIGURATION
###########################################################################

# Some handy constants. These become global variables in this script. Using the
# naming convention of all caps to remind us that these are not to be changed.

# MAX_STRING_LEN is good with 10 characters. This is the length of the long format
# for date and time. Most of the id labels are 6 or 8 characters.
MAX_STRING_LEN = 10

# MAX_EVENTS will usually be limited to 255 due to an array size in the Fortran
# interface. In practice, there are typically a handful of events (4 or 5) since
# the events are related to the steps that are gone through to convert a raw
# BUFR file to a prepBUFR file (at NCEP). It is generally accepted that 20 is
# a safe limit (instead of 255) for the max number of events, so set MaxEvents
# to 20 to help conserve file space.
MAX_EVENTS = 20

# MAX_LEVELS should be limited to 255 by a Fortran array size. This may need to
# change in the future since this number corresponds to the number of atmospheric
# levels in an observation.
MAX_LEVELS = 255

# BUFR file types
BFILE_UNDEF    = 0
BFILE_BUFR     = 1
BFILE_PREPBUFR = 2

# BUFR types
BTYPE_UNDEF  = 0
BTYPE_HEADER = 1
BTYPE_DATA   = 2
BTYPE_EVENT  = 3
BTYPE_REP    = 4

# Data types
DTYPE_UNDEF   = 0
DTYPE_STRING  = 1   # for CCITT IA5 units in the BUFR table
DTYPE_INTEGER = 2   # for CODE TABLE, FLAG TABLE units in the BUFR table
DTYPE_FLOAT   = 3   # for all other units in the BUFR table
DTYPE_UINT    = 4   # for dimension coordinates
DTYPE_DOUBLE  = 5   # temporary: for strings that are expected to be double
                    #            in downstream flows (GSI)

############################################################################
# CLASSES
############################################################################

# The BUFR format is extremely flexible, and different obs types have taken
# advantage of that fact. This has resulted in the requirement of utilizing
# different algorithms to extract obs data for different obs types. Ie, it's
# extremely difficult to force the different formats into a common algorithm.
# Using a base class with a simple extraction algorithm which can be overridden
# in a derived class seems to be a good way to handle this situation.
#
# For the extraction, it does appear that many obs types will place a header
# at the front of an BUFR subset which consists of a simple list of BUFR
# mnemonics. The header is followed by the obs data which can be a simple
# list of mnemonics, but typically is a more complex structure with 
# replications, sequences and events. The header extraction algorithm can
# (for now) belong in the base class, and the obs data extraction algorithms
# can belong in the derived classes (ie, specific to each obs type).
#
# Define the base class with a simple method that assumes all variables have a
# one-to-one corrspondence with a BUFR mnemonic. More complex examples can
# override the convert(), convert_header(), and/or convert_obs() methods
# and do whatever is necessary. The thought about breaking up convert() into
# two methods, convert_header() and convert_obs(), is that it seems that most
# obs types will be able to utilize the simple convert_header() algorithm.
#
# The format for an entry in the *_spec lists is:
#
#    [ nc_varname, mnemonic, data_type, dim_names, dim_sizes, created ] 
#
#        nc_varname: netcdf variable name
#        mnemonic:   BUFR mnemonic
#        data_type:  float, integer, string, ...
#        dim_names:  (list of dimension names)
#        dim_sizes:  (list of dimension sizes)
#        created:    flag: True  - nc variable has been created
#                          False - nc variable has not been created
#

################################# Base Observation Type ############################
class ObsType(object):
    ### initialize data elements ###
    def __init__(self):
        self.bufr_ftype = BFILE_UNDEF
        self.mtype_re = 'UnDef'
        self.nobs = -1
        self.time_units = ""
        self.hdr_spec = []
        self.int_spec = []
        self.evn_spec = []
        self.rep_spec = []
        self.seq_spec = []
        self.dim_spec = []

    ### methods ###

    ###############################################################################
    # This method will set the time units. The time units value will be used to
    # calculate time offsets for observation values.
    def set_time_units(self, tunits):
        self.time_units = tunits

    ###############################################################################
    # This method will set the number of observations. This must be called
    # before attempting to create any netcdf variables since self.nobs
    # is also used to define the dimension sizes in all of the netcdf variables.
    def set_nobs(self, nobs):
        # update the data memeber
        self.nobs = nobs

        # update the dimension sizes in the specs
        #
        # each spec is a list of variable specs
        # each variable spec is a list with the fourth item being a list of
        #    dimension names and the fifth item being a list of dimension sizes
        #
        # for every place in the dimension name list where the name is 'nobs', replace
        # the corresponding size in the size list with self.nobs
        for spec in [ self.hdr_spec, self.int_spec, self.evn_spec,
                      self.rep_spec, self.seq_spec, self.dim_spec ]:
            for var_spec in spec:
                for i in [ j for j,dname in enumerate(var_spec[3]) if dname == 'nobs']:
                    var_spec[4][i] = self.nobs

    ###############################################################################
    # This method will set the dimension specs (data memeber self.dim_spec). The
    # format for the dim_spec will match that of the other specs (eg, self.hdr_spec).
    def init_dim_spec(self):
        # Do a union on all of the dimension names.
        AllDimNames = set([])
        for spec in [ self.hdr_spec, self.int_spec, self.evn_spec, self.rep_spec, self.seq_spec ]:
            for var_spec in spec:
                AllDimNames = AllDimNames | set(var_spec[3])

        # AllDimNames holds the list of unique dimension names.
        self.dim_spec = []
        for dname in AllDimNames:
            if (dname == 'nobs'):
                dsize = self.nobs
            elif (dname == 'nlevs'):
                dsize = MAX_LEVELS
            elif (dname == 'nevents'):
                dsize = MAX_EVENTS
            elif (dname == 'nstring'):
                dsize = MAX_STRING_LEN
            else:
                print("ERROR: init_dim_spec: Unknown dimension name: {0:s}".format(dname))
                sys.exit(3)

            self.dim_spec.append([ dname, dname, DTYPE_UINT, [ dname ], [ dsize], False ])

    ###############################################################################
    # This method will create dimensions and variables in the netcdf file
    # according to the obs type variable specs.
    def create_nc_datasets(self, fid):

        # Create dimensions first so that the variables can reference them.
        for dspec in self.dim_spec:
            nc.createDimension(dspec[0], dspec[4][0])

        # Create variables including the coordinates for the dimensions
        for spec in [ self.dim_spec, self.hdr_spec, self.int_spec, self.evn_spec, self.rep_spec, self.seq_spec ]:
            for var_spec in spec:
                Vname    = var_spec[0]
                Dtype    = var_spec[2]
                DimNames = var_spec[3]
                DimSizes = var_spec[4]

                # Convert the data type code to a netCDF data type
                if (Dtype == DTYPE_STRING):
                    Vtype = 'S1'
                elif (Dtype == DTYPE_INTEGER):
                    Vtype = 'i4'
                elif (Dtype == DTYPE_UINT):
                    Vtype = 'u4'
                elif (Dtype == DTYPE_FLOAT):
                    Vtype = 'f4'
                elif (Dtype == DTYPE_DOUBLE):
                    Vtype = 'f8'

                # For the chunk sizes,
                #   The first dimension is always nobs
                #   For vars with a single dimension,
                #       make the chunk spec match dim sizes
                #   For vars with multiple dimensions,
                #       make the chunk spec match dim sizes except use 1 for the first entry
                if (len(DimSizes) == 1):
                    ChunkSizes = DimSizes
                else:
                    ChunkSizes = [ 1 ] + DimSizes[1:]

                nc.createVariable(Vname, Vtype, DimNames, chunksizes=ChunkSizes)


    ###############################################################################
    # This method will convert the BUFR data into netcdf data. This includes
    # reading BUFR and writing netcdf. This method is broken down into two
    # steps: convert the header data and convert the obs data.
    #
    # Note: if you need a more complex algorithm to convert data, any of the three
    # following methods (covert, convert_header, convert_obs) can be overridden
    # in the derived class.
    def convert(self):
        self.convert_header()
        self.convert_obs()

    ###############################################################################
    # The simple version of converting the header data
    def convert_header(self):
        print("DEBUG: Converting header data (base): ")
        print("DEBUG: hdr_spec: ", self.hdr_spec)

    ###############################################################################
    # The simple version of converting the observation data
    def convert_obs(self):
        print("DEBUG: Converting obs data (base): ")
        print("DEBUG: int_spec: ", self.int_spec)
        print("DEBUG: evn_spec: ", self.evn_spec)
        print("DEBUG: rep_spec: ", self.rep_spec)
        print("DEBUG: seq_spec: ", self.seq_spec)
        print("DEBUG: dim_spec: ", self.dim_spec)


################################# Aircraft Observation Type ############################
class AircraftObsType(ObsType):
    ### initialize data elements ###
    def __init__(self, bf_type):
        super().__init__()

        self.nstring = MAX_STRING_LEN
        self.nevents = MAX_EVENTS
        self.bufr_ftype = bf_type
        if (bf_type == BFILE_BUFR):
            self.mtype_re = '^NC004001'
            self.hdr_spec = [
                [ 'YEAR', 'YEAR', DTYPE_INTEGER, ['nobs'], [self.nobs] ],
                [ 'MNTH', 'MNTH', DTYPE_INTEGER, ['nobs'], [self.nobs] ],
                [ 'DAYS', 'DAYS', DTYPE_INTEGER, ['nobs'], [self.nobs] ],
                [ 'HOUR', 'HOUR', DTYPE_INTEGER, ['nobs'], [self.nobs] ],
                [ 'MINU', 'MINU', DTYPE_INTEGER, ['nobs'], [self.nobs] ],
                [ 'ACID', 'ACID', DTYPE_DOUBLE,  ['nobs'], [self.nobs] ],
                [ 'CORN', 'CORN', DTYPE_INTEGER, ['nobs'], [self.nobs] ],
                [ 'CLAT', 'CLAT', DTYPE_FLOAT,   ['nobs'], [self.nobs] ],
                [ 'CLON', 'CLON', DTYPE_FLOAT,   ['nobs'], [self.nobs] ],
                [ 'FLVL', 'FLVL', DTYPE_FLOAT,   ['nobs'], [self.nobs] ],
                ]
            self.int_spec = [
                [ 'TMDB',   'TMDB',   DTYPE_FLOAT,   ['nobs'], [self.nobs] ],
                [ 'TMDP',   'TMDP',   DTYPE_FLOAT,   ['nobs'], [self.nobs] ],
                [ 'REHU',   'REHU',   DTYPE_FLOAT,   ['nobs'], [self.nobs] ],
                [ 'WSPD',   'WSPD',   DTYPE_FLOAT,   ['nobs'], [self.nobs] ],
                [ 'WDIR',   'WDIR',   DTYPE_FLOAT,   ['nobs'], [self.nobs] ],
                [ 'QMAT',   'QMAT',   DTYPE_INTEGER, ['nobs'], [self.nobs] ],
                [ 'QMDD',   'QMDD',   DTYPE_INTEGER, ['nobs'], [self.nobs] ],
                [ 'QMWN',   'QMWN',   DTYPE_INTEGER, ['nobs'], [self.nobs] ],
                [ 'SEQNUM', 'SEQNUM', DTYPE_STRING,  ['nobs', 'nstring'], [self.nobs, self.nstring] ],
                [ 'BUHD',   'BUHD',   DTYPE_STRING,  ['nobs', 'nstring'], [self.nobs, self.nstring] ],
                [ 'BORG',   'BORG',   DTYPE_STRING,  ['nobs', 'nstring'], [self.nobs, self.nstring] ],
                [ 'BULTIM', 'BULTIM', DTYPE_STRING,  ['nobs', 'nstring'], [self.nobs, self.nstring] ],
                [ 'BBB',    'BBB',    DTYPE_STRING,  ['nobs', 'nstring'], [self.nobs, self.nstring] ],
                [ 'RPID',   'RPID',   DTYPE_STRING,  ['nobs', 'nstring'], [self.nobs, self.nstring] ],
                ]
            self.evn_spec = []
            self.rep_spec = []
            self.seq_spec = []
        elif (bf_type == BFILE_PREPBUFR):
            self.mtype_re = 'AIRC[AF][RT]'
            self.hdr_spec = [
                [ 'SID',  'SID',  DTYPE_DOUBLE,  ['nobs'], [self.nobs] ],
                [ 'ACID', 'ACID', DTYPE_DOUBLE,  ['nobs'], [self.nobs] ],
                [ 'XOB',  'XOB',  DTYPE_FLOAT,   ['nobs'], [self.nobs] ],
                [ 'YOB',  'YOB',  DTYPE_FLOAT,   ['nobs'], [self.nobs] ],
                [ 'DHR',  'DHR',  DTYPE_FLOAT,   ['nobs'], [self.nobs] ],
                [ 'TYP',  'TYP',  DTYPE_INTEGER, ['nobs'], [self.nobs] ],
                [ 'ELV',  'ELV',  DTYPE_FLOAT,   ['nobs'], [self.nobs] ],
                [ 'SAID', 'SAID', DTYPE_INTEGER, ['nobs'], [self.nobs] ],
                [ 'T29',  'T29',  DTYPE_INTEGER, ['nobs'], [self.nobs] ],
                ]
            self.int_spec = [
                [ 'POB',  'POB',  DTYPE_FLOAT,   ['nobs'], [self.nobs] ],
                [ 'QOB',  'QOB',  DTYPE_FLOAT,   ['nobs'], [self.nobs] ],
                [ 'TOB',  'TOB',  DTYPE_FLOAT,   ['nobs'], [self.nobs] ],
                [ 'ZOB',  'ZOB',  DTYPE_FLOAT,   ['nobs'], [self.nobs] ],
                [ 'UOB',  'UOB',  DTYPE_FLOAT,   ['nobs'], [self.nobs] ],
                [ 'VOB',  'VOB',  DTYPE_FLOAT,   ['nobs'], [self.nobs] ],
                [ 'PWO',  'PWO',  DTYPE_FLOAT,   ['nobs'], [self.nobs] ],
                [ 'MXGS', 'MXGS', DTYPE_FLOAT,   ['nobs'], [self.nobs] ],
                [ 'PRSS', 'PRSS', DTYPE_FLOAT,   ['nobs'], [self.nobs] ],
                [ 'TDO',  'TDO',  DTYPE_FLOAT,   ['nobs'], [self.nobs] ],
                [ 'PMO',  'PMO',  DTYPE_FLOAT,   ['nobs'], [self.nobs] ],
                [ 'PQM',  'PQM',  DTYPE_INTEGER, ['nobs'], [self.nobs] ],
                [ 'QQM',  'QQM',  DTYPE_INTEGER, ['nobs'], [self.nobs] ],
                [ 'TQM',  'TQM',  DTYPE_INTEGER, ['nobs'], [self.nobs] ],
                [ 'ZQM',  'ZQM',  DTYPE_INTEGER, ['nobs'], [self.nobs] ],
                [ 'WQM',  'WQM',  DTYPE_INTEGER, ['nobs'], [self.nobs] ],
                [ 'PWQ',  'PWQ',  DTYPE_INTEGER, ['nobs'], [self.nobs] ],
                [ 'PMQ',  'PMQ',  DTYPE_INTEGER, ['nobs'], [self.nobs] ],
                [ 'POE',  'POE',  DTYPE_FLOAT,   ['nobs'], [self.nobs] ],
                [ 'QOE',  'QOE',  DTYPE_FLOAT,   ['nobs'], [self.nobs] ],
                [ 'TOE',  'TOE',  DTYPE_FLOAT,   ['nobs'], [self.nobs] ],
                [ 'WOE',  'WOE',  DTYPE_FLOAT,   ['nobs'], [self.nobs] ],
                [ 'PWE',  'PWE',  DTYPE_FLOAT,   ['nobs'], [self.nobs] ],
                [ 'HOVI', 'HOVI', DTYPE_FLOAT,   ['nobs'], [self.nobs] ],
                [ 'CAT',  'CAT',  DTYPE_INTEGER, ['nobs'], [self.nobs] ],
                [ 'XDR',  'XDR',  DTYPE_FLOAT,   ['nobs'], [self.nobs] ],
                [ 'YDR',  'YDR',  DTYPE_FLOAT,   ['nobs'], [self.nobs] ],
                [ 'HRDR', 'HRDR', DTYPE_FLOAT,   ['nobs'], [self.nobs] ],
                [ 'POAF', 'POAF', DTYPE_INTEGER, ['nobs'], [self.nobs] ],
                [ 'IALR', 'IALR', DTYPE_FLOAT,   ['nobs'], [self.nobs] ],
                ]
            self.evn_spec = [
                [ 'TPC_bevn', 'TPC', DTYPE_INTEGER, ['nobs', 'nevents'], [self.nobs, self.nevents] ],
                [ 'TOB_bevn', 'TOB', DTYPE_FLOAT,   ['nobs', 'nevents'], [self.nobs, self.nevents] ],
                [ 'TQM_bevn', 'TQM', DTYPE_INTEGER, ['nobs', 'nevents'], [self.nobs, self.nevents] ],
                ]
            self.rep_spec = []
            self.seq_spec = []

        # Set the dimension specs.
        super().init_dim_spec()

    ### methods ###
    

################################# Radiosonde Observation Type ############################
class SondesObsType(ObsType):
    ### initialize data elements ###
    def __init__(self, bf_type):
        super().__init__()

        self.nlevs   = MAX_LEVELS
        self.nevents = MAX_EVENTS
        self.bufr_ftype = bf_type
        if (bf_type == BFILE_BUFR):
            self.mtype_re = 'UnDef'
            self.hdr_spec = []
            self.int_spec = []
            self.evn_spec = []
            self.rep_spec = []
            self.seq_spec = []
        elif (bf_type == BFILE_PREPBUFR):
            # Clara: THIS LIST IS NOT EXHAUSTIVE!!!!
            #        it is based on dumping a few messages, 
            #        then screening for vars read in by the gsi
            #          1. Header
            #          2. Obs types
            #          3. quality markers
            #          4. error ests.
            #          5. location info?
            #
            # Clara: PREPBUFR FILES INCLUDE (BUT NOT READ BY GSI): 
            #            'TSB',  'ITP',  'SQN','PROCN',  'RPT', 'TCOR', 'SIRC',
            #        EVENTS VARS? *PC, *RC, *FC , TVO
            #
            self.mtype_re = 'ADPUPA'
            self.hdr_spec = [
                [ 'SID', 'SID', DTYPE_DOUBLE,  ['nobs', 'nlevs'], [self.nobs, self.nlevs] ],
                [ 'XOB', 'XOB', DTYPE_FLOAT,   ['nobs', 'nlevs'], [self.nobs, self.nlevs] ],
                [ 'YOB', 'YOB', DTYPE_FLOAT,   ['nobs', 'nlevs'], [self.nobs, self.nlevs] ],
                [ 'DHR', 'DHR', DTYPE_FLOAT,   ['nobs', 'nlevs'], [self.nobs, self.nlevs] ],
                [ 'TYP', 'TYP', DTYPE_INTEGER, ['nobs', 'nlevs'], [self.nobs, self.nlevs] ],
                [ 'ELV', 'ELV', DTYPE_FLOAT,   ['nobs', 'nlevs'], [self.nobs, self.nlevs] ],
                [ 'T29', 'T29', DTYPE_INTEGER, ['nobs', 'nlevs'], [self.nobs, self.nlevs] ],
                ]
            self.int_spec = [
                [ 'POB',  'POB',  DTYPE_FLOAT,   ['nobs', 'nlevs'], [self.nobs, self.nlevs] ],
                [ 'QOB',  'QOB',  DTYPE_FLOAT,   ['nobs', 'nlevs'], [self.nobs, self.nlevs] ],
                [ 'TOB',  'TOB',  DTYPE_FLOAT,   ['nobs', 'nlevs'], [self.nobs, self.nlevs] ],
                [ 'ZOB',  'ZOB',  DTYPE_FLOAT,   ['nobs', 'nlevs'], [self.nobs, self.nlevs] ],
                [ 'UOB',  'UOB',  DTYPE_FLOAT,   ['nobs', 'nlevs'], [self.nobs, self.nlevs] ],
                [ 'VOB',  'VOB',  DTYPE_FLOAT,   ['nobs', 'nlevs'], [self.nobs, self.nlevs] ],
                [ 'PWO',  'PWO',  DTYPE_FLOAT,   ['nobs', 'nlevs'], [self.nobs, self.nlevs] ],
                [ 'TDO',  'TDO',  DTYPE_FLOAT,   ['nobs', 'nlevs'], [self.nobs, self.nlevs] ],
                [ 'PQM',  'PQM',  DTYPE_INTEGER, ['nobs', 'nlevs'], [self.nobs, self.nlevs] ],
                [ 'QQM',  'QQM',  DTYPE_INTEGER, ['nobs', 'nlevs'], [self.nobs, self.nlevs] ],
                [ 'TQM',  'TQM',  DTYPE_INTEGER, ['nobs', 'nlevs'], [self.nobs, self.nlevs] ],
                [ 'ZQM',  'ZQM',  DTYPE_INTEGER, ['nobs', 'nlevs'], [self.nobs, self.nlevs] ],
                [ 'WQM',  'WQM',  DTYPE_INTEGER, ['nobs', 'nlevs'], [self.nobs, self.nlevs] ],
                [ 'PWQ',  'PWQ',  DTYPE_INTEGER, ['nobs', 'nlevs'], [self.nobs, self.nlevs] ],
                [ 'PMQ',  'PMQ',  DTYPE_INTEGER, ['nobs', 'nlevs'], [self.nobs, self.nlevs] ],
                [ 'POE',  'POE',  DTYPE_FLOAT,   ['nobs', 'nlevs'], [self.nobs, self.nlevs] ],
                [ 'QOE',  'QOE',  DTYPE_FLOAT,   ['nobs', 'nlevs'], [self.nobs, self.nlevs] ],
                [ 'TOE',  'TOE',  DTYPE_FLOAT,   ['nobs', 'nlevs'], [self.nobs, self.nlevs] ],
                [ 'WOE',  'WOE',  DTYPE_FLOAT,   ['nobs', 'nlevs'], [self.nobs, self.nlevs] ],
                [ 'PWE',  'PWE',  DTYPE_FLOAT,   ['nobs', 'nlevs'], [self.nobs, self.nlevs] ],
                [ 'XDR',  'XDR',  DTYPE_FLOAT,   ['nobs', 'nlevs'], [self.nobs, self.nlevs] ],
                [ 'YDR',  'YDR',  DTYPE_FLOAT,   ['nobs', 'nlevs'], [self.nobs, self.nlevs] ],
                [ 'HRDR', 'HRDR', DTYPE_FLOAT,   ['nobs', 'nlevs'], [self.nobs, self.nlevs] ],
                ]
            self.evn_spec = [
                [ 'TPC_bevn', 'TPC', DTYPE_INTEGER, ['nobs', 'nlevs', 'nevents'], [self.nobs, self.nlevs, self.nevents] ],
                [ 'TOB_bevn', 'TOB', DTYPE_FLOAT,   ['nobs', 'nlevs', 'nevents'], [self.nobs, self.nlevs, self.nevents] ],
                [ 'TQM_bevn', 'TQM', DTYPE_INTEGER, ['nobs', 'nlevs', 'nevents'], [self.nobs, self.nlevs, self.nevents] ],
                ]
            self.rep_spec = []
            self.seq_spec = []

        # Set the dimension specs.
        super().init_dim_spec()

    ### methods ###
    

########################### Radiance (AMSU-A) Observation Type ############################
class AmsuaObsType(ObsType):
    ### initialize data elements ###
    def __init__(self, bf_type):
        super().__init__()

        self.nchans = 20  # This is unique to AMSU
        self.bufr_ftype = bf_type
        if (bf_type == BFILE_BUFR):

            self.mtype_re = '^NC021023'
            self.hdr_spec = [
                [ 'SAID', 'SAID', DTYPE_INTEGER, ['nobs'], [self.nobs] ],
                [ 'FOVN', 'FOVN', DTYPE_INTEGER, ['nobs'], [self.nobs] ],
                [ 'YEAR', 'YEAR', DTYPE_INTEGER, ['nobs'], [self.nobs] ],
                [ 'MNTH', 'MNTH', DTYPE_INTEGER, ['nobs'], [self.nobs] ],
                [ 'DAYS', 'DAYS', DTYPE_INTEGER, ['nobs'], [self.nobs] ],
                [ 'HOUR', 'HOUR', DTYPE_INTEGER, ['nobs'], [self.nobs] ],
                [ 'MINU', 'MINU', DTYPE_INTEGER, ['nobs'], [self.nobs] ],
                [ 'SECO', 'SECO', DTYPE_INTEGER, ['nobs'], [self.nobs] ],
                [ 'CLAT', 'CLAT', DTYPE_FLOAT,   ['nobs'], [self.nobs] ],
                [ 'CLON', 'CLON', DTYPE_FLOAT,   ['nobs'], [self.nobs] ],
                [ 'HOLS', 'HOLS', DTYPE_FLOAT,   ['nobs'], [self.nobs] ],
                ]
            self.int_spec = [
                [ 'SAZA',   'SAZA',   DTYPE_FLOAT, ['nobs'], [self.nobs] ],
                [ 'SOZA',   'SOZA',   DTYPE_FLOAT, ['nobs'], [self.nobs] ],
                [ 'BEARAZ', 'BEARAZ', DTYPE_FLOAT, ['nobs'], [self.nobs] ],
                [ 'SOLAZI', 'SOLAZI', DTYPE_FLOAT, ['nobs'], [self.nobs] ],
                ]
            self.evn_spec = []
            self.rep_spec = [
                [ 'CHNM', 'CHNM', DTYPE_INTEGER, ['nobs', 'nchans'], [self.nobs, self.nchans] ],
                [ 'TMBR', 'TMBR', DTYPE_FLOAT,   ['nobs', 'nchans'], [self.nobs, self.nchans] ],
                [ 'CSTC', 'CSTC', DTYPE_FLOAT,   ['nobs', 'nchans'], [self.nobs, self.nchans] ],
                ]
            self.seq_spec = []
        elif (bf_type == BFILE_PREPBUFR):
            self.mtype_re = 'UnDef'
            self.hdr_spec = []
            self.int_spec = []
            self.evn_spec = []
            self.rep_spec = []
            self.seq_spec = []

        # Set the dimension specs.
        super().init_dim_spec()

    ### methods ###
    

###########################################################################
# SUBROUTINES
###########################################################################

def SplitDate(yyyymmddhh):
    # This routine will take an integer date yyyymmddhh and return the
    # datetime equivalent.
    DateString = str(yyyymmddhh)
    Dtime = dt.datetime(int(DateString[0:4]), int(DateString[4:6]),
                        int(DateString[6:8]), int(DateString[8:10]))

    return Dtime

def MakeDate(Dtime):
    # This routine will take in integers representing yyyy, mm, dd, hh and
    # return an integer date yyyymmddhh.

    DateString = "%0.4i"%(Dtime.year) + "%0.2i"%(Dtime.month) + "%0.2i"%(Dtime.day) + "%0.2i"%(Dtime.hour)

    return int(DateString)

def FindRefDate(StartDate):
    # This routine will return the next analysis time (0, 6, 12, 18Z) following
    # the date represented in StartDate. StartDate is an integer value of the form
    # yyyymmddhh.
    #
    # Use datetime structures so that addition will take into account carry
    # over into the next day, month, year when hours are added.
    #
    # Compare the hours field in the start date with the analysis times
    # 0, 6, 12, 18, 24 where 24 is 0Z of the next day. 24 is there to make
    # sure that when subtracting the start date hour value, at least one
    # of the entries in the result will be greater than zero. Then the
    # distance to the next analysis time will be the first entry that is
    # greater than zero in the result of the subtraction (HourDiffs).
    StartDtime = SplitDate(StartDate)
    HourDiffs = np.array([ 0, 6, 12, 18, 24 ]) - StartDtime.hour
    HourInc = int(HourDiffs[HourDiffs > 0][0])

    # Form the datetime compatible version of HourInc, which can then
    # be added to the start date.
    DtDelta = dt.timedelta(hours=HourInc) 
    RefDtime = StartDtime + DtDelta
    RefDate = MakeDate(RefDtime)

    return RefDate

def BfilePreprocess(BufrFname, MessageRe, MaxNumMsg):
    # This routine will read the BUFR file and figure out how many observations
    # will be read when recording data.
    #
    # It will also figure out the timestamp to use for the obs reference time.
    # The next analysis time (0, 6, 12, 18Z) past the earliest message date
    # found in the selected messages will be used for the reference time.
    #
    # The msg_date value is an integer in the form of YYYYMMDDHH. Because of
    # this format, the smallest integer is also the earliest date. So we
    # just need to find the minimum date value and then figure out the
    # next analysis time from that value.

    bufr = ncepbufr.open(BufrFname)

    # The number of observations will be equal to the total number of subsets
    # contained in the selected messages.
    TotalNumMsg = 0
    NumMsg = 0 
    NumObs = 0 
    EarliestDate = 9999999999 # A value about 8000 years from now
    LatestDate = 0
    while ( (bufr.advance() == 0) ): 
        # Select only the messages that belong to this observation type
        if (re.search(MessageRe, bufr.msg_type)):
            TotalNumMsg += 1

            # If MaxNumMsg is less than 1, then select all messages.
            # If MaxNumMsg is >= 1, then select no more than MaxNumMsg
            if ((MaxNumMsg < 1) or (NumMsg < MaxNumMsg)):
                # Attribute "subsets" contains the number of subsets
                # for the current message.
                NumMsg += 1
                NumObs += bufr._subsets()

                if (bufr.msg_date < EarliestDate):
                    EarliestDate = bufr.msg_date

    bufr.close()

    return [NumObs, NumMsg, TotalNumMsg, FindRefDate(EarliestDate)] 

###################################################################################
# MAIN
###################################################################################
ScriptName = os.path.basename(sys.argv[0])

# Parse command line
ap = argparse.ArgumentParser()
ap.add_argument("obs_type", help="observation type")
ap.add_argument("input_bufr", help="path to input BUFR file")
ap.add_argument("output_netcdf", help="path to output netCDF4 file")
ap.add_argument("-m", "--maxmsgs", type=int, default=-1,
                help="maximum number of messages to keep", metavar="<max_num_msgs>")
ap.add_argument("-c", "--clobber", action="store_true",
                help="allow overwrite of output netcdf file")
ap.add_argument("-p", "--prepbufr", action="store_true",
                help="input BUFR file is in prepBUFR format")

MyArgs = ap.parse_args()

ObsType = MyArgs.obs_type
BufrFname = MyArgs.input_bufr
NetcdfFname = MyArgs.output_netcdf
MaxNumMsg = MyArgs.maxmsgs
ClobberOfile = MyArgs.clobber
if (MyArgs.prepbufr):
    BfileType = BFILE_PREPBUFR
else:
    BfileType = BFILE_BUFR

# Check files
BadArgs = False
if (not os.path.isfile(BufrFname)): 
    print("ERROR: {0:s}: Specified input BUFR file does not exist: {1:s}".format(ScriptName, BufrFname))
    print("")
    BadArgs = True

if (os.path.isfile(NetcdfFname)):
    if (ClobberOfile):
        print("WARNING: {0:s}: Overwriting nc file: {1:s}".format(ScriptName, NetcdfFname))
        print("")
    else:
        print("ERROR: {0:s}: Specified nc file already exists: {1:s}".format(ScriptName, NetcdfFname))
        print("ERROR: {0:s}:   Use -c option to overwrite.".format(ScriptName))
        print("")
        BadArgs = True

# Check the observation type, and create an observation instance.
if (ObsType == 'Aircraft'):
    Obs = AircraftObsType(BfileType)
elif (ObsType == 'Sondes'):
    Obs = SondesObsType(BfileType)
elif (ObsType == 'Amsua'):
    Obs = AmsuaObsType(BfileType)
else:
    print("ERROR: {0:s}: Unknown observation type: {1:s}".format(ScriptName, ObsType))
    print("")
    BadArgs = True

print("DEBUG: Obs object: ", Obs, dir(Obs))
print("DEBUG: Obs.nobs: ", Obs.nobs)
print("DEBUG: Obs.mtype_re: ", Obs.mtype_re)

if (not BadArgs):
    if (Obs.mtype_re == 'UnDef'):
        if (BfileType == BFILE_BUFR):
            print("ERROR: {0:s}: Observation type {1:s} for BUFR format is undefined".format(ScriptName, ObsType))
        elif (BfileType == BFILE_PREPBUFR):
            print("ERROR: {0:s}: Observation type {1:s} for prepBUFR format is undefined".format(ScriptName, ObsType))
        print("")
        BadArgs = True

if (BadArgs):
    sys.exit(2)

# Arguments are okay, and we've got an observation object instantiated. Note that
# we need to have the obs object instantiated before calling BfilePreprocess()
# routine below. This is so BfilePreprocess() can select messages in the
# same manner as the subsequent conversion.
print("Converting BUFR to netCDF")
print("  Observation Type: {0:s}".format(ObsType))
if (BfileType == BFILE_BUFR):
    print("  Input BUFR file (BUFR format): {0:s}".format(BufrFname))
elif (BfileType == BFILE_PREPBUFR):
    print("  Input BUFR file (prepBUFR format): {0:s}".format(BufrFname))
print("  Output netCDF file: {0:s}".format(NetcdfFname))
if (MaxNumMsg > 0):
    print("  Limiting nubmer of messages to record to {0:d} messages".format(MaxNumMsg))
print("")

# It turns out that using multiple unlimited dimensions in the netCDF file
# can be very detrimental to the file's size, and can also be detrimental
# to the runtime for creating the file.
#
# In order to mitigate this, we want to use fixed size dimensions instead.
# Each obs type object will have its associated dimension sizes defined as
# fixed sizes. The only missing part is how many observations (subsets) will
# be selected.
#
# This number of subsets needs to be determined from reading through all the
# selected messages. Fortunately, this is very fast.
#
# Make a pass through the BUFR file to determine the number of observations
# and the reference time.
#
# BfilePreprocess() will use the regular expression for selecting message
# types. NumObs will be set to the number of observations selected,
# NumMsgs will be set to the number of messages selected, and TotalMsgs
# will be set to the total number of messages that match Obs.mtype_re in the file.
[NumObs, NumMsgs, TotalMsgs, RefDate] = BfilePreprocess(BufrFname, Obs.mtype_re, MaxNumMsg)

print("  Total number of messages that match obs type {0:s}: {1:d}".format(ObsType, TotalMsgs))
print("  Number of messages selected: {0:d}".format(NumMsgs))
print("  Number of observations selected: {0:d}".format(NumObs))
print("  Reference date for observations: {0:d}".format(RefDate))
print("")

# We will use the netCDF4 method date2num to calculate the datetime offset from the
# reference date (RefDate). This method wants a datetime object holding the current
# date and time as the first argument, and a units string holding the reference time.
# The units string is in the form "seconds since YYYY-MM-DD HH:00 UTC" where YYYY, MM,
# DD and HH come from the reference date. Create the units string and add that to the
# Obs object.
RefDtime = SplitDate(RefDate)
TimeUnits = "seconds since %0.4i-%0.2i-%0.2i %0.2i:00 UTC" %(RefDtime.year,
            RefDtime.month, RefDtime.day, RefDtime.hour)
Obs.set_time_units(TimeUnits)

print("DEBUG: time units: ", Obs.time_units)

# Now that we have the number of observations we will be recording, set the dimension
# size in the obs object. Note the set_nobs() method needs to be called before creating
# netcdf variables.
Obs.set_nobs(NumObs)

# Create the dimensions and variables in the netCDF file in preparation for
# recording the selected observations.
nc = Dataset(NetcdfFname, 'w', format='NETCDF4')
Obs.create_nc_datasets(nc)


Obs.convert()

nc.sync()
nc.close()

### ###########################################################################
### # SUBROUTINES
### ###########################################################################
### 
### ###########################################################################
### def ExtractBufrData(Bval, Dname, Btype, Dtype):
###     # This routine will extract the value of a variable from the
###     # output of read_subset(). read_subset() will return a floating point
###     # number (for any type) or an empty list if the mnemonic didn't exist. For strings
###     # (Dtype = DTYPE_STRING) read the floating point number as characters. Otherwise
###     # convert to integer or leave alone.
###     #
###     # Keep Dtype values in sync with entries in the DATA_TYPES dictionary. For now,
###     # these values are DTYPE_STRING, DTYPE_INTEGER and DTYPE_FLOAT.
### 
###     # If the incoming Bval is empty, then set DataPresent to False. This will tell
###     # ReadWriteNcVar to skip writing this value into the output file, which means
###     # that Dval can remain unset.
###     DataPresent = (Bval.size > 0)
###     if (DataPresent):
###         # Bval is not empty. Convert the Bval data to the appropriate type, and
###         # return another masked array with the proper data and mask.
### 
###         if (Dtype == DTYPE_STRING):
###             # convert to list of strings
###             # assume that an ID is a 1D array of float with only one
###             # entry
###             #
###             # The bytes.join().decode() method wants the byte values
###             # < 127 so that they can be mapped to the old style ascii
###             # character set. In order to accommodate this, unpack the
###             # float value into bytes. Then check the bytes and replace
###             # all values > 127 with a blank character. Then convert
###             # the byte lists to strings. Replace byte value
###             # equal to zero with a blank as well.
###             ByteList = list(struct.unpack('8c', Bval))
### 
###             # replace chars < 1 and > 127 with blank space
###             for j in range(len(ByteList)):
###                 ByteVal = struct.unpack('@B', ByteList[j])[0]
###                 if ( (ByteVal < 1) or (ByteVal > 127)):
###                     ByteList[j] = b' '
### 
###             TempStr = bytes.join(b'', ByteList).decode('ascii') 
###             Dval = np.ma.array(TempStr, mask=Bval.mask, dtype='S8')
###         elif (Dtype == DTYPE_INTEGER):
###             # convert to integer
###             Dval = np.ma.array(Bval.data.astype(np.int32), mask=Bval.mask, dtype=np.int32)
###         elif (Dtype == DTYPE_FLOAT):
###             # copy floats
###             Dval = np.ma.array(Bval.data.astype(np.float32), mask=Bval.mask, dtype=np.float32)
###         elif (Dtype == DTYPE_DOUBLE):
###             # copy doubles
###             Dval = np.ma.array(Bval.data.astype(np.float64), mask=Bval.mask, dtype=np.float64)
### 
###     return [Dval, DataPresent] 
### 
### ###########################################################################
### def WriteNcVar(Fid, obs_num, Dname, Btype, Dval, MaxStringLen, MaxEvents, MaxReps):
###     # This routine will write into a variable in the output netCDF file
### 
###     # Set the variable name according to Btype
###     if (Btype == BTYPE_EVENT):
###         Vname = "{0:s}_bevn".format(Dname)
###     else:
###         Vname = Dname
### 
###     # For the string data, convert to a numpy character array
###     if ((Dval.dtype.char == 'S') or (Dval.dtype.char == 'U')):
###         IsString=True
###         StrSpec = "S{0:d}".format(MaxStringLen)
###         Value = netCDF4.stringtochar(Dval.astype(StrSpec))
###     else:
###         IsString=False
###         if (Btype == BTYPE_EVENT):
###             # Trim the dimension representing events to 0:MaxEvents.
###             # This will be the last dimension of a multi-dim array:
###             #    either [nlev, nevent], or [nlev, nstring, nevent].
###             Value = Dval[...,0:MaxEvents].copy()
###         elif (Btype == BTYPE_REP):
###             # Trim the dimension representing reps to 0:MaxReps.
###             # This will be the last dimension of a multi-dim array:
###             #    either [nlev, nrep], or [nlev, nstring, nrep].
###             Value = Dval[...,0:MaxReps].copy()
###         else:
###             Value = Dval.copy()
### 
###     # Write the variable. Since the dimension sizes from the read_subset()
###     # routine can vary, we need to use array slice style indexing to
###     # copy Value into netCDF variable (NcVar below). Look at how many
###     # dimensions Value has compared to NcVar and put in the appropriate
###     # slice indexing. nobs (obs_num) is always the first dimension.
###     NcVar = Fid[Vname]
###     ValNdim = Value.ndim
###     NcNdim = NcVar.ndim
###     if (ValNdim == 1):
###         if (NcNdim == 1):
###             # Value has one dimension (scalar)
###             # NcVar has one dimension (eg, [nobs])
###             NcVar[obs_num] = Value
###         else:
###             # Value has one dimension  (eg, [nlevs])
###             # NcVar has two dimensions (eg, [nobs,nlevs])
###             N1 = Value.shape[0]
###             NcVar[obs_num,0:N1] = Value
###     elif (ValNdim == 2):
###         # Value has two dimensions   (eg, [nlevs,nevents])
###         # NcVar has three dimensions (eg, [nobs,nlevs,nevents])
###         N1 = Value.shape[0]
###         N2 = Value.shape[1]
###         NcVar[obs_num,0:N1,0:N2] = Value
###     elif (ValNdim == 3):
###         # Value has three dimensions (eg, [nlevs,nstring,nevents])
###         # NcVar has four dimensions  (eg, [nobs,nlevs,nstring,nevents])
###         N1 = Value.shape[0]
###         N2 = Value.shape[1]
###         N3 = Value.shape[2]
###         NcVar[obs_num,0:N1,0:N2,0:N3] = Value
### 
### ###########################################################################
### def ReadWriteGroup(Fid, Mlist, Btype, DataTypes, MaxStringLen, MaxEvents, MaxReps):
###     # This routine will read the mnemonics from the bufr file, convert them to
###     # their proper data types and write them into the output netCDF file.
###     Mstring = " ".join(Mlist)
###     Eflag = (Btype == BTYPE_EVENT)
###     Rflag = (Btype == BTYPE_REP)
###     # CSD-keep this as a masked array for handling NaNs.
###     BufrVals = Fid.read_subset(Mstring, events=Eflag, rep=Rflag)
### 
###     for i, Vname in enumerate(Mlist):
###         Bval = BufrVals[i,...]
###         [VarVal, VarInBufr] = ExtractBufrData(Bval, Vname, Btype, DataTypes[Vname])
###         if VarInBufr:
###             WriteNcVar(nc, NumObs, Vname, Btype, VarVal, MaxStringLen, MaxEvents, MaxReps)
### 
### ###########################################################################
### # MAIN
### ###########################################################################
### 
### NumMsgs= 0
### NumSelectedMsgs = 0
### NumObs = 0
### while ( (bufr.advance() == 0) and (NumSelectedMsgs < NMsgRead)): 
###     NumMsgs += 1
###     MsgType = np.array(bufr.msg_type)
###     MsgDate = np.array([bufr.msg_date])
### 
###     # Select only the messages that belong to this observation type
###     if (re.search(MessageRe, bufr.msg_type)):
###         # Write out obs into the netCDF file as they are read from
###         # the BUFR file. Need to start with index zero in the netCDF
###         # file so don't increment the counter until after the write.
###         while (bufr.load_subset() == 0):
###             # Record message type and date with each subset. This is
###             # inefficient in storage (lots of redundancy), but is the
###             # expected format for now.
###             WriteNcVar(nc, NumObs, MtypeVname, BTYPE_HEADER, MsgType, MaxStringLen, MaxEvents, MaxReps)
###             WriteNcVar(nc, NumObs, MdateVname, BTYPE_HEADER, MsgDate, MaxStringLen, MaxEvents, MaxReps)
### 
###             # Read mnemonics in sets that make one call to read_subset(). This should help
###             # reduce overhead and help this script run faster. After read_subset() is called,
###             # BufrVals will be an array with its first dimension being the mnemonic number.
###             # If the string passed to read_subset() is 'TOB POB QOB', then the first dimension
###             # of BufrVals will have a size of 3, and BufrVals[0,...] will be the data for TOB,
###             # BufrVals[1,...] for POB, and BufrVals[2,...] for QOB.
### 
###             # Header mnemonics
###             if (len(HeadList) > 0):
###                 ReadWriteGroup(bufr, HeadList, BTYPE_HEADER, DATA_TYPES, MaxStringLen, MaxEvents, MaxReps)
### 
###             # Observation mnemonics
###             if (len(ObsList) > 0):
###                 ReadWriteGroup(bufr, ObsList, BTYPE_DATA, DATA_TYPES, MaxStringLen, MaxEvents, MaxReps)
### 
###             # Quality mark mnemonics
###             if (len(QmarkList) > 0):
###                 ReadWriteGroup(bufr, QmarkList, BTYPE_DATA, DATA_TYPES, MaxStringLen, MaxEvents, MaxReps)
### 
###             # Error mnemonics
###             if (len(ErrList) > 0):
###                 ReadWriteGroup(bufr, ErrList, BTYPE_DATA, DATA_TYPES, MaxStringLen, MaxEvents, MaxReps)
### 
###             # Misc mnemonics
###             if (len(MiscList) > 0):
###                 ReadWriteGroup(bufr, MiscList, BTYPE_DATA, DATA_TYPES, MaxStringLen, MaxEvents, MaxReps)
### 
###             # Event mnemonics
###             if (len(EventList) > 0):
###                 ReadWriteGroup(bufr, EventList, BTYPE_EVENT, DATA_TYPES, MaxStringLen, MaxEvents, MaxReps)
### 
###             # Rep mnemonics
###             if (len(RepList) > 0):
###                 ReadWriteGroup(bufr, RepList, BTYPE_REP, DATA_TYPES, MaxStringLen, MaxEvents, MaxReps)
### 
###             NumObs += 1
### 
### 
###         NumSelectedMsgs += 1
### 
### 
### # Fill in coordinate values. Simply put in the numbers 1 through N for each
### # dimension variable according to that dimension's size.
### nc[NobsDname][0:MaxObs]       = np.arange(MaxObs) + 1
### nc[NlevsDname][0:MaxLevels]   = np.arange(MaxLevels) + 1
### nc[NeventsDname][0:MaxEvents] = np.arange(MaxEvents) + 1
### nc[NrepsDname][0:MaxReps]     = np.arange(MaxReps) + 1
### nc[StrDname][0:MaxStringLen]  = np.arange(MaxStringLen) + 1
### 
### # If reading a prepBUFR type file, then record the virtual temperature code
### if (BufrFtype == 'prepBUFR'):
###     nc.virtmp_code = bufr.get_program_code('VIRTMP')
### 
### print("{0:d} messages selected out of {1:d} total messages".format(MaxMsgs, TotalMsgs))
### print("  {0:d} observations recorded in output netCDF file".format(MaxObs))
### 
### 
### bufr.close()
### 
