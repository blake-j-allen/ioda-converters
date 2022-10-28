#!/usr/bin/python

"""
Python code to ingest netCDF4 or HDF5 ATMS data
"""

import argparse
from datetime import datetime, timedelta
import glob
# from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
import os.path
from os import getcwd
import sys

import h5py
import numpy as np

IODA_CONV_PATH = Path(__file__).parent/"@SCRIPT_LIB_PATH@"
if not IODA_CONV_PATH.is_dir():
    IODA_CONV_PATH = Path(__file__).parent/'..'/'lib-python'
sys.path.append(str(IODA_CONV_PATH.resolve()))
import ioda_conv_engines as iconv
from orddicts import DefaultOrderedDict

# globals
SNPP_WMO_sat_ID = 224
NOAA20_WMO_sat_ID = 225
NOAA21_WMO_sat_ID = 226

locationKeyList = [
    ("latitude", "float"),
    ("longitude", "float"),
    ("datetime", "string")
]


def main(args):

    output_filename = args.output
    dtg = datetime.strptime(args.date, '%Y%m%d%H')

    input_files = [(i) for i in args.input]
    # read / process files in parallel
    obs_data = {}
    # create a thread pool
#   with ProcessPoolExecutor(max_workers=args.threads) as executor:
#       for file_obs_data in executor.map(get_data_from_files, input_files):
#           if not file_obs_data:
#               print("INFO: non-nominal file skipping")
#               continue
#           if obs_data:
#               concat_obs_dict(obs_data, file_obs_data)
#           else:
#               obs_data = file_obs_data

    for afile in input_files:
        file_obs_data = get_data_from_files(afile)
        if not file_obs_data:
            print("INFO: non-nominal file skipping")
            continue
        if obs_data:
            concat_obs_dict(obs_data, file_obs_data)
        else:
            obs_data = file_obs_data

    GlobalAttrs = get_global_attributes('atms')
    
    write_obs_2ioda(obs_data)

def write_obs_2ioda(obs_data, GlobalAttrs):
# V2 nlocs_int32 = np.array(len(obs_data[('latitude', 'MetaData')]), dtype='int32')
    nlocs_int32 = np.array(len(obs_data[('latitude', 'MetaData')]), dtype='float32')  # this is float32 in old convention
    nlocs = nlocs_int32.item()
    nchans = len(obs_data[('channelNumber', 'MetaData')])

    # prepare global attributes we want to output in the file,
    # in addition to the ones already loaded in from the input file
    GlobalAttrs['date_time_string'] = dtg.strftime("%Y-%m-%dT%H:%M:%SZ")
    date_time_int32 = np.array(int(dtg.strftime("%Y%m%d%H")), dtype='int32')
    GlobalAttrs['date_time'] = date_time_int32.item()
    GlobalAttrs['converter'] = os.path.basename(__file__)

    # pass parameters to the IODA writer
# V2     'brightnessTemperature': ['nlocs', 'nchans']
    VarDims = {
        'brightness_temperature': ['nlocs', 'nchans'],
        'channelNumber': ['nchans'],
    }

    DimDict = {
        'nlocs': nlocs,
        'nchans': obs_data[('channelNumber', 'MetaData')],
    }
    writer = iconv.IodaWriter(output_filename, locationKeyList, DimDict)

    VarAttrs = DefaultOrderedDict(lambda: DefaultOrderedDict(dict))
# V2 VarAttrs[('brightnessTemperature', 'ObsValue')]['units'] = 'K'
# V2 VarAttrs[('brightnessTemperature', 'ObsError')]['units'] = 'K'
# V2 VarAttrs[('brightnessTemperature', 'PreQC')]['units'] = 'unitless'
    VarAttrs[('brightness_temperature', 'ObsValue')]['units'] = 'K'
    VarAttrs[('brightness_temperature', 'ObsError')]['units'] = 'K'
    VarAttrs[('brightness_temperature', 'PreQC')]['units'] = 'unitless'

    missing_value = 9.96921e+36
    int_missing_value = -2147483647
# V2 VarAttrs[('brightnessTemperature', 'ObsValue')]['_FillValue'] = missing_value
# V2 VarAttrs[('brightnessTemperature', 'ObsError')]['_FillValue'] = missing_value
# V2 VarAttrs[('brightnessTemperature', 'PreQC')]['_FillValue'] = int_missing_value
    VarAttrs[('brightness_temperature', 'ObsValue')]['_FillValue'] = missing_value
    VarAttrs[('brightness_temperature', 'ObsError')]['_FillValue'] = missing_value
    VarAttrs[('brightness_temperature', 'PreQC')]['_FillValue'] = int_missing_value

#   there can be more than one input file
#   GlobalAttrs['converter'] = os.path.basename(__file__)
    # final write to IODA file
    writer.BuildIoda(obs_data, VarDims, VarAttrs, GlobalAttrs)


def get_data_from_files(zfiles):

    # allocate space for output depending on which variables are to be saved
    obs_data = init_obs_loc()

    # for afile in zfiles:
    afile = zfiles
    if True:
        f = h5py.File(afile, 'r')

        path, file = os.path.split(afile)
        if afile[-3:] == 'nc4':
            g = h5py.File(afile, 'r')
        else:
            geo_file = file.replace('SATMS', 'GATMO')
            ftype, sat, date, time, end_time, orbit, _, _, _ = file.split('_')
            geo_search = '_'.join(['GATMO', sat, date, time, end_time, orbit, '*.h5'])
            geo_search = os.path.join(path, geo_search)

            gfile = glob.glob(geo_search)
            if (len(gfile) > 0):
                g = h5py.File(gfile[0], 'r')
            else:
                print("could not find geofile matching: %s" % filename)
                print("tried searching geofile: %s" % geo_search)
                sys.exit()

        obs_data = get_data(f, g, obs_data)
        f.close()
        g.close()

    return obs_data


def get_data(f, g, obs_data):

    # NOAA CLASS h5 SDR-GEO keys
    # 'BeamLatitude', 'BeamLongitude', 'Height', 'Latitude', 'Longitude', 'MidTime', 'PadByte1',
    # 'QF1_ATMSSDRGEO', 'SCAttitude', 'SCPosition', 'SCVelocity', 'SatelliteAzimuthAngle',
    # 'SatelliteRange', 'SatelliteZenithAngle', 'SolarAzimuthAngle', 'SolarZenithAngle', 'StartTime'

    # NOAA CLASS h5 SDR-Data keys
    # 'BeamTime', 'BrightnessTemperature', 'BrightnessTemperatureFactors', 'GainCalibration',
    # 'InstrumentMode', 'NEdTCold', 'NEdTWarm', 'PadByte1', 'QF10_GRAN_HEALTHSTATUS',
    # 'QF11_GRAN_QUADRATICCORRECTION', 'QF12_SCAN_KAVPRTCONVERR', 'QF13_SCAN_WGPRTCONVERR',
    # 'QF14_SCAN_SHELFPRTCONVERR', 'QF15_SCAN_KAVPRTTEMPLIMIT', 'QF16_SCAN_WGPRTTEMPLIMIT',
    # 'QF17_SCAN_KAVPRTTEMPCONSISTENCY', 'QF18_SCAN_WGPRTTEMPCONSISTENCY', 'QF19_SCAN_ATMSSDR',
    # 'QF1_GRAN_HEALTHSTATUS', 'QF20_ATMSSDR', 'QF21_ATMSSDR', 'QF22_ATMSSDR',
    # 'QF2_GRAN_HEALTHSTATUS', 'QF3_GRAN_HEALTHSTATUS', 'QF4_GRAN_HEALTHSTATUS',
    # 'QF5_GRAN_HEALTHSTATUS', 'QF6_GRAN_HEALTHSTATUS', 'QF7_GRAN_HEALTHSTATUS',
    # 'QF8_GRAN_HEALTHSTATUS', 'QF9_GRAN_HEALTHSTATUS'

    # NASA GES DISC keys
    # 'antenna', 'antenna_len', 'antenna_temp', 'antenna_temp_qc', 'asc_flag', 'asc_node_local_solar_time',
    # 'asc_node_lon', 'asc_node_tai93', 'atrack', 'attitude', 'attitude_lbl', 'attitude_lbl_len',
    # 'aux_cal_blackbody_qualflag', 'aux_cal_qualflag', 'aux_cal_space_qualflag', 'aux_cold_temp',
    # 'aux_gain', 'aux_geo_qualflag', 'aux_nonlin', 'aux_offset', 'aux_warm_temp', 'band', 'band_geoloc_chan',
    # 'band_land_frac', 'band_lat', 'band_lat_bnds', 'band_lbl', 'band_lbl_len', 'band_lon', 'band_lon_bnds',
    # 'band_surf_alt', 'bandwidth', 'beam_width', 'center_freq', 'chan_band', 'chan_band_len', 'channel',
    # 'cold_nedt', 'fov_poly', 'if_offset_1', 'if_offset_2', 'instrument_state', 'land_frac', 'lat',
    # 'lat_bnds', 'lat_geoid', 'local_solar_time', 'lon', 'lon_bnds', 'lon_geoid', 'mean_anom_wrt_equat',
    # 'moon_ang', 'obs_id', 'obs_id_len', 'obs_time_tai93', 'obs_time_utc', 'polarization', 'polarization_len',
    # 'sat_alt', 'sat_att', 'sat_azi', 'sat_pos', 'sat_range', 'sat_sol_azi', 'sat_sol_zen', 'sat_vel', 'sat_zen',
    # 'scan_mid_time', 'sol_azi', 'sol_zen', 'solar_beta_angle', 'spacextrack', 'spatial', 'spatial_lbl',
    # 'spatial_lbl_len', 'subsat_lat', 'subsat_lon', 'sun_glint_dist', 'sun_glint_lat', 'sun_glint_lon',
    # 'surf_alt', 'surf_alt_sdev', 'utc_tuple', 'utc_tuple_lbl', 'utc_tuple_lbl_len', 'view_ang', 'warm_nedt', 'xtrack'

    WMO_sat_ID = get_WMO_satellite_ID(f.filename)

    # example: dimension ( 180, 96 ) == dimension( nscan, nbeam_pos )
    try:
        nscans = np.shape(g['lat'])[0]
        nbeam_pos = np.shape(g['lat'])[1]
        obs_data[('latitude', 'MetaData')] = np.array(g['lat'][:, :].flatten(), dtype='float32')
        obs_data[('longitude', 'MetaData')] = np.array(g['lon'][:, :].flatten(), dtype='float32')
        obs_data[('channelNumber', 'MetaData')] = np.array(g['channel'][:], dtype='int32')
# V2     obs_data[('fieldOfViewNumber', 'MetaData')] = np.tile(np.arange(nbeam_pos, dtype='int32') + 1, (nscans, 1)).flatten()
        obs_data[('scan_position', 'MetaData')] = np.tile(np.arange(nbeam_pos, dtype='float32') + 1, (nscans, 1)).flatten()
# V2     obs_data[('solarZenithAngle', 'MetaData')] = np.array(g['sol_zen'][:, :].flatten(), dtype='float32')
        obs_data[('solar_zenith_angle', 'MetaData')] = np.array(g['sol_zen'][:, :].flatten(), dtype='float32')
# V2     obs_data[('solarAzimuthAngle', 'MetaData')] = np.array(g['sol_azi'][:, :].flatten(), dtype='float32')
        obs_data[('solar_azimuth_angle', 'MetaData')] = np.array(g['sol_azi'][:, :].flatten(), dtype='float32')
# V2     obs_data[('sensorZenithAngle', 'MetaData')] = np.array(g['sat_zen'][:, :].flatten(), dtype='float32')
        obs_data[('sensor_zenith_angle', 'MetaData')] = np.array(g['sat_zen'][:, :].flatten(), dtype='float32')
# V2     obs_data[('sensorAzimuthAngle', 'MetaData')] = np.array(g['sat_azi'][:, :].flatten(), dtype='float32')
        obs_data[('sensor_azimuth_angle', 'MetaData')] = np.array(g['sat_azi'][:, :].flatten(), dtype='float32')
        obs_data[('sensor_view_angle', 'MetaData')] = np.array(g['view_ang'][:, :].flatten(), dtype='float32')
        nlocs = len(obs_data[('latitude', 'MetaData')])
        obs_data[('satelliteId', 'MetaData')] = np.full((nlocs), WMO_sat_ID, dtype='int32')
        obs_data[('datetime', 'MetaData')] = np.array(get_string_dtg(g['obs_time_utc'][:, :, :]), dtype=object)

    # BaseException is a catch-all mechamism
    except BaseException:
        # this section is for the NOAA CLASS files and need to be tested
        obs_data[('latitude', 'MetaData')] = np.array(g['All_Data']['ATMS-SDR-GEO_All']['Latitude'][:, :].flatten(), dtype='float32')
        obs_data[('longitude', 'MetaData')] = np.array(g['All_Data']['ATMS-SDR-GEO_All']['Longitude'][:, :].flatten(), dtype='float32')

    # example: dimension ( 180, 96, 22 ) == dimension( nscan, nbeam_pos, nchannel )
    try:
        nchans = len(obs_data[('channelNumber', 'MetaData')])
        nlocs = len(obs_data[('latitude', 'MetaData')])
# V2     obs_data[('brightnessTemperature', "ObsValue")] = np.array(np.vstack(g['antenna_temp']), dtype='float32')
# V2     obs_data[('brightnessTemperature', "ObsError")] = np.full((nlocs, nchans), 5.0, dtype='float32')
# V2     obs_data[('brightnessTemperature', "PreQC")] = np.full((nlocs, nchans), 0, dtype='int32')
        obs_data[('brightness_temperature', "ObsValue")] = np.array(np.vstack(g['antenna_temp']), dtype='float32')
        obs_data[('brightness_temperature', "ObsError")] = np.full((nlocs, nchans), 5.0, dtype='float32')
        obs_data[('brightness_temperature', "PreQC")] = np.full((nlocs, nchans), 0, dtype='int32')
    except BaseException:
        # this section is for the NOAA CLASS files and need to be tested
        scaled_data = np.vstack(f['All_Data']['ATMS-SDR_All']['BrightnessTemperature'])
        scale_fac = f['All_Data']['ATMS-SDR_All']['BrightnessTemperatureFactors'][:].flatten()

        obs_data[('brightnessTemperature', "ObsValue")] = np.array((scaled_data * scale_fac[0]) + scale_fac[1], dtype='float32')
        obs_data[('brightnessTemperature', "ObsError")] = np.full((nlocs, nchans), 5.0, dtype='float32')
        obs_data[('brightnessTemperature', "PreQC")] = np.full((nlocs, nchans), 0, dtype='int32')

    return obs_data


def get_WMO_satellite_ID(filename):

    afile = os.path.basename(filename)
    if 'SNPP' in afile or 'npp' in afile:
        WMO_sat_ID = SNPP_WMO_sat_ID
    elif 'J1' in afile or 'j01' in afile:
        WMO_sat_ID = NOAA20_WMO_sat_ID
    elif 'J2' in afile or 'j02' in afile:
        WMO_sat_ID = NOAA21_WMO_sat_ID
    else:
        WMO_sat_ID = -1
        print("could not determine satellite from filename: %s" % afile)
        sys.exit()

    return WMO_sat_ID


def get_string_dtg(obs_time_utc):

    year = obs_time_utc[:, :, 0].flatten()
    month = obs_time_utc[:, :, 1].flatten()
    day = obs_time_utc[:, :, 2].flatten()
    hour = obs_time_utc[:, :, 3].flatten()
    minute = obs_time_utc[:, :, 4].flatten()
    dtg = []
    for i, yyyy in enumerate(year):
        cdtg = ("%4i-%.2i-%.2iT%.2i:%.2i:00Z" % (yyyy, month[i], day[i], hour[i], minute[i]))
        dtg.append(cdtg)

    return dtg


def init_obs_loc():
    # V2     ('brightnessTemperature', "ObsValue"): [],
    # V2     ('brightnessTemperature', "ObsError"): [],
    # V2     ('brightnessTemperature', "PreQC"): [],
    # V2     ('fieldOfViewNumber', 'MetaData'): [],
    # V2     ('solarZenithAngle', 'MetaData'): [],
    # V2     ('solarAzimuthAngle', 'MetaData'): [],
    # V2     ('sensorZenithAngle', 'MetaData'): [],
    # V2     ('sensorAzimuthAngle', 'MetaData'): [],
    obs = {
        ('brightness_temperature', "ObsValue"): [],
        ('brightness_temperature', "ObsError"): [],
        ('brightness_temperature', "PreQC"): [],
        ('satelliteId', 'MetaData'): [],
        ('channelNumber', 'MetaData'): [],
        ('latitude', 'MetaData'): [],
        ('longitude', 'MetaData'): [],
        ('datetime', 'MetaData'): [],
        ('scan_position', 'MetaData'): [],
        ('solar_zenith_angle', 'MetaData'): [],
        ('solar_azimuth_angle', 'MetaData'): [],
        ('sensor_zenith_angle', 'MetaData'): [],
        ('sensor_view_angle', 'MetaData'): [],
        ('sensor_azimuth_angle', 'MetaData'): [],
    }

    return obs


def get_global_attributes(sensor):

    if 'atms':
        GlobalAttrs = {
            "platformCommonName": "ATMS",
            "platformLongDescription": "ATMS Brightness Temperature Data",
            "sensorCentralFrequency": [
                23.8, 31.4, 50.3, 51.76, 52.8, 53.596, 54.40, 54.94, 55.50,
                57.2903, 57.2903, 57.2903, 57.2903, 57.2903, 57.2903,
                88.20, 165.5, 183.31, 183.31, 183.31, 183.31, 183.31],
            }
    elif 'airs':
        aqua_freq= np.array( [  
            15.394, 15.366, 15.360, 15.343, 15.337, 15.315, 15.309,  
            15.303, 15.286, 15.281, 15.275, 15.264, 15.247, 15.241,  
            15.230, 15.196, 15.179, 15.173, 15.162, 15.111, 15.105,  
            15.094, 15.088, 15.083, 15.066, 15.049, 15.043, 15.015,  
            15.009, 14.998, 14.992, 14.986, 14.981, 14.975, 14.969,  
            14.964, 14.958, 14.952, 14.947, 14.935, 14.930, 14.924,  
            14.913, 14.879, 14.873, 14.845, 14.839, 14.828, 14.811,  
            14.805, 14.788, 14.777, 14.771, 14.760, 14.743, 14.737,  
            14.703, 14.697, 14.674, 14.669, 14.503, 14.498, 14.469,  
            14.464, 14.435, 14.429, 14.401, 14.395, 14.384, 14.367,  
            14.350, 14.333, 14.327, 14.321, 14.310, 14.304, 14.298,  
            14.293, 14.281, 14.270, 14.264, 14.253, 14.236, 14.230,  
            14.207, 14.196, 14.190, 14.162, 14.144, 14.127, 14.110,  
            14.093, 14.076, 14.065, 14.059, 14.047, 14.030, 14.013,  
            14.002, 13.996, 13.968, 13.928, 13.876, 13.865, 13.859,  
            13.854, 13.848, 13.831, 13.825, 13.802, 13.796, 13.768,  
            13.739, 13.621, 13.598, 13.593, 13.564, 13.547, 13.541,  
            13.536, 13.490, 13.473, 13.450, 13.405, 13.376, 13.279,  
            13.239, 13.165, 12.608, 12.483, 12.432, 12.358, 12.183,  
            11.850, 11.477, 10.901, 10.884, 10.662, 10.546, 10.358,  
            10.213,  9.986,  9.948,  9.918,  9.896,  9.871,  9.836,  
             9.794,  9.704,  9.661,  9.648,  9.644,  9.623,  9.614,  
             9.605,  9.593,  9.469,  9.439,  9.422,  9.418,  9.405,  
             9.401,  9.388,  9.358,  9.324,  9.307,  9.154,  9.065,  
             9.035,  8.971,  8.904,  8.840,  8.806,  8.217,  8.207,  
             8.166,  8.142,  8.125,  8.087,  8.077,  7.991,  7.779,  
             7.742,  7.680,  7.677,  7.673,  7.670,  7.629,  7.598,  
             7.513,  7.493,  7.464,  7.450,  7.433,  7.428,  7.402,  
             7.368,  7.314,  7.311,  7.260,  7.240,  7.183,  7.158,  
             7.132,  7.103,  7.044,  7.007,  6.981,  6.958,  6.935,  
             6.808,  6.794,  6.774,  6.737,  6.697,  6.671,  6.654,  
             6.606,  6.583,  6.574,  6.560,  6.483,  6.475,  6.460,  
             6.443,  6.435,  6.426,  6.395,  6.378,  6.369,  6.361,  
             6.344,  6.304,  6.256,  6.230,  4.584,  4.582,  4.580,  
             4.578,  4.576,  4.571,  4.569,  4.565,  4.563,  4.561,  
             4.554,  4.552,  4.550,  4.548,  4.523,  4.516,  4.497,  
             4.485,  4.483,  4.478,  4.474,  4.472,  4.464,  4.447,  
             4.445,  4.443,  4.440,  4.430,  4.428,  4.426,  4.407,  
             4.383,  4.379,  4.350,  4.337,  4.230,  4.228,  4.208,  
             4.206,  4.204,  4.203,  4.201,  4.198,  4.196,  4.192,  
             4.191,  4.189,  4.187,  4.186,  4.184,  4.182,  4.180,  
             4.179,  4.177,  4.175,  4.174,  4.172,  4.170,  4.168,  
             4.167,  4.165,  4.163,  4.155,  4.145,  4.133,  4.088,  
             4.081,  4.074,  4.055,  4.013,  3.999,  3.979,  3.949,  
             3.936,  3.905,  3.845,  3.841,  3.835,  3.830,  3.822,  
             3.813,  3.799,  3.791,  3.788,  3.785,  3.775,  3.764,  
             3.763, 3.754 ] )
        airs_freq = 1.e4/aqua_freq
        GlobalAttrs = {
            "platformCommonName": "AIRS",
            "platformLongDescription": "AIRS Brightness Temperature Data",
            "sensorCentralFrequency": airs_freq,
            }
    else:
        raise NotImplementedError(f" ... ERROR get_global_attributes: unknown sensor: {sensor}")

    return GlobalAttrs


def concat_obs_dict(obs_data, append_obs_data):
    # For now we are assuming that the obs_data dictionary has the "golden" list
    # of variables. If one is missing from append_obs_data, a warning will be issued.
    append_keys = list(append_obs_data.keys())
    for gv_key in obs_data.keys():
        if gv_key in append_keys:
            obs_data[gv_key] = np.append(obs_data[gv_key], append_obs_data[gv_key], axis=0)
        else:
            print("WARNING: ", gv_key, " is missing from append_obs_data dictionary")


if __name__ == "__main__":

    parser = argparse.ArgumentParser(
        description=(
            'Reads the satellite data '
            ' convert into IODA formatted output files. '
            ' Multiple files are concatenated')
    )

    required = parser.add_argument_group(title='required arguments')
    required.add_argument(
        '-i', '--input',
        help="path of satellite observation input file(s)",
        type=str, nargs='+', required=True)
    required.add_argument(
        '-d', '--date',
        metavar="YYYYMMDDHH",
        help="base date for the center of the window",
        type=str, required=True)

    optional = parser.add_argument_group(title='optional arguments')
    optional.add_argument(
        '-j', '--threads',
        help='multiple threads can be used to load input files in parallel.'
             ' (default: %(default)s)',
        type=int, default=1)
    optional.add_argument(
        '-o', '--output',
        help='path to output ioda file',
        type=str, default=os.getcwd())

    args = parser.parse_args()

    main(args)
