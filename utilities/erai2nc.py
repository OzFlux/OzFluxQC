import datetime
import glob
import netCDF4
import numpy
import os
import pytz
from scipy.interpolate import InterpolatedUnivariateSpline
import sys
# check the scripts directory is present
if not os.path.exists("../scripts/"):
    print "erai2nc: the scripts directory is missing"
    sys.exit()
# since the scripts directory is there, try importing the modules
sys.path.append('../scripts')
import meteorologicalfunctions as mf
import pysolar
import qcio
import qcutils

cf_list = sorted(glob.glob("../controlfiles/ERAI/*"))
for cf_name in cf_list:
    print "Processing control file "+cf_name
    cf = qcio.get_controlfilecontents(cf_name)

    erai_name = os.path.join(cf["Files"]["erai_path"],cf["Files"]["erai_file"])
    erai_file = netCDF4.Dataset(erai_name)
    latitude = erai_file.variables["latitude"][:]
    longitude = erai_file.variables["longitude"][:]
    lat_resolution = abs(latitude[-1]-latitude[0])/(len(latitude)-1)
    lon_resolution = abs(longitude[-1]-longitude[0])/(len(longitude)-1)
    # get the time and convert to Python datetime object
    erai_time = erai_file.variables["time"][:]
    time_units = getattr(erai_file.variables["time"],"units")
    dt_erai = netCDF4.num2date(erai_time,time_units)
    hour_utc = numpy.array([dt.hour for dt in dt_erai])
    erai_timestep = 180
    
    # get a list of sites
    site_list = cf["Sites"].keys()
    #site_list = ["Calperum"]
    # now loop over the sies
    for site in site_list:
        # get the output file name
        if not os.path.exists(cf["Sites"][site]["out_filepath"]):
            os.makedirs(cf["Sites"][site]["out_filepath"])
        out_filename = os.path.join(cf["Sites"][site]["out_filepath"],
                                   cf["Sites"][site]["out_filename"])
        # get the metadata from the control file
        site_name = cf["Sites"][site]["site_name"]
        print " Processing "+site_name
        site_timezone = cf["Sites"][site]["site_timezone"]
        site_latitude = float(cf["Sites"][site]["site_latitude"])
        site_longitude = float(cf["Sites"][site]["site_longitude"])
        site_timestep = int(cf["Sites"][site]["site_timestep"])
        site_sa_limit = qcutils.get_keyvaluefromcf(cf,["Sites",site],site_sa_limit,default=5)
        # index of the site in latitude dimension
        site_lat_index = int(((latitude[0]-site_latitude)/lat_resolution)+0.5)
        erai_latitude = latitude[site_lat_index]
        # index of the site in longitude dimension
        site_lon_index = int(((site_longitude-longitude[0])/lon_resolution)+0.5)
        erai_longitude = longitude[site_lon_index]
        # get an instance of the Datastructure
        ds_erai = qcio.DataStructure()
        ds_erai.series["DateTime"] = {}
        ds_erai.globalattributes["site_name"] = site_name
        ds_erai.globalattributes["time_zone"] = site_timezone
        ds_erai.globalattributes["latitude"] = site_latitude
        ds_erai.globalattributes["longitude"] = site_longitude
        ds_erai.globalattributes["time_step"] = site_timestep
        ds_erai.globalattributes["sa_limit"] = site_sa_limit
        ds_erai.globalattributes['xl_datemode'] = str(0)
        ds_erai.globalattributes["nc_level"] = "L1"
        # get the UTC and local datetime series
        site_tz = pytz.timezone(site_timezone)
        # make utc_dt timezone aware
        dt_erai_utc = [x.replace(tzinfo=pytz.utc) for x in dt_erai]
        # get local time from UTC
        dt_erai_loc = [x.astimezone(site_tz) for x in dt_erai_utc]
        # NOTE: will have to disable daylight saving at some stage, towers stay on Standard Time
        # PRI hopes that the following line will do this ...
        dt_erai_loc = [x-x.dst() for x in dt_erai_loc]
        # make local time timezone naive to match datetimes in OzFluxQC
        dt_erai_loc_ntz = [x.replace(tzinfo=None) for x in dt_erai_loc]
        dt_erai_utc_ntz = [x.replace(tzinfo=None) for x in dt_erai_utc]
        # get the datetime in the middle of the accumulation period
        erai_offset = datetime.timedelta(minutes=float(erai_timestep)/2)
        # subtract this from the ERA-I datetime to align ERA-I 3 hourly points with the middle
        # of the accumulation period
        dt_erai_loc_cor = [x - erai_offset for x in dt_erai_loc_ntz]
        dt_erai_utc_cor = [x - erai_offset for x in dt_erai_utc_ntz]
        # now we get the datetime series at the tower time step
        tdts = datetime.timedelta(minutes=site_timestep)
        # local time for plotting
        start_date = dt_erai_loc_cor[0]
        end_date = dt_erai_loc_cor[-1]
        dt_erai_loc_tts = [x for x in qcutils.perdelta(start_date,end_date,tdts)]
        ds_erai.series["DateTime"]["Data"] = dt_erai_loc_tts
        ds_erai.globalattributes["nc_nrecs"] = len(dt_erai_loc_tts)
        ds_erai.globalattributes["start_datetime"] = str(dt_erai_loc_tts[0])
        ds_erai.globalattributes["end_datetime"] = str(dt_erai_loc_tts[-1])
        # get the Excel datetime
        qcutils.get_xldatefromdatetime(ds_erai)
        # get the year, month, day, hour, minute and second
        qcutils.get_ymdhmsfromdatetime(ds_erai)
        # UTC for calculating solar altitude
        start_date = dt_erai_utc_cor[0]
        end_date = dt_erai_utc_cor[-1]
        dt_erai_utc_tts = [x for x in qcutils.perdelta(start_date,end_date,tdts)]
        # UTC at tower time step as number for interpolation
        erai_time_tts = netCDF4.date2num(dt_erai_utc_tts,time_units)
        erai_time_3hr = netCDF4.date2num(dt_erai_utc_cor,time_units)
        # get the solar altitude, we will use this later to interpolate the ERA Interim
        # data from the ERA-I 3 hour time step to the tower time step.
        # NOTE: alt_solar is in degrees
        alt_solar_3hr = numpy.array([pysolar.GetAltitude(erai_latitude,erai_longitude,dt) for dt in dt_erai_utc_cor])
        # get the solar altitude at the tower time step
        alt_solar_tts = numpy.array([pysolar.GetAltitude(erai_latitude,erai_longitude,dt) for dt in dt_erai_utc_tts])
        idx = numpy.where(alt_solar_tts<=0)[0]
        alt_solar_tts[idx] = float(0)
        
        # Interpolate the 3 hourly accumulated downwelling shortwave to the tower time step
        # NOTE: ERA-I variables are dimensioned [time,latitude,longitude]
        Fsd_3d = erai_file.variables["ssrd"][:,:,:]
        Fsd_accum = Fsd_3d[:,site_lat_index,site_lon_index]
        # Downwelling shortwave in ERA-I is a cummulative value that is reset to 0 at 0300 and 1500 UTC.
        # Here we convert the cummulative values to 3 hourly values.
        Fsd_erai_3hr = numpy.ediff1d(Fsd_accum,to_begin=0)
        idx = numpy.where((hour_utc==3)|(hour_utc==15))[0]
        Fsd_erai_3hr[idx] = Fsd_accum[idx]
        Fsd_erai_3hr = Fsd_erai_3hr/(erai_timestep*60)
        # normalise the ERA-I downwelling shortwave by the solar altitude
        # clamp solar altitude to a minimum value to avoid numerical problems
        # when alt_solar is close to 0
        alt_solar_limit = float(site_sa_limit)*numpy.ones(len(alt_solar_3hr))
        sa = numpy.where(alt_solar_3hr<=float(site_sa_limit),alt_solar_limit,alt_solar_3hr)
        coef_3hr = Fsd_erai_3hr/numpy.sin(numpy.deg2rad(sa))
        # get the spline interpolation function
        s = InterpolatedUnivariateSpline(erai_time_3hr, coef_3hr, k=1)
        # get the coefficient at the tower time step
        coef_tts = s(erai_time_tts)
        # get the downwelling solar radiation at the tower time step
        Fsd_erai_tts = coef_tts*numpy.sin(numpy.deg2rad(alt_solar_tts))
        flag = numpy.zeros(len(Fsd_erai_tts),dtype=numpy.int32)
        attr = qcutils.MakeAttributeDictionary(long_name="Downwelling short wave radiation",units="W/m2")
        qcutils.CreateSeries(ds_erai,"Fsd",Fsd_erai_tts,Flag=flag,Attr=attr)
        
        # Interpolate the 3 hourly accumulated net shortwave to the tower time step
        # NOTE: ERA-I variables are dimensioned [time,latitude,longitude]
        Fn_sw_3d = erai_file.variables["ssr"][:,:,:]
        Fn_sw_accum = Fn_sw_3d[:,site_lat_index,site_lon_index]
        # Net shortwave in ERA-I is a cummulative value that is reset to 0 at 0300 and 1500 UTC.
        # Here we convert the cummulative values to 3 hourly values.
        Fn_sw_erai_3hr = numpy.ediff1d(Fn_sw_accum,to_begin=0)
        # deal with the reset times at 0300 and 1500
        idx = numpy.where((hour_utc==3)|(hour_utc==15))[0]
        Fn_sw_erai_3hr[idx] = Fn_sw_accum[idx]
        # get the average value over the 3 hourly period
        Fn_sw_erai_3hr = Fn_sw_erai_3hr/(erai_timestep*60)
        # normalise the ERA-I et shortwave by the solar altitude
        coef_3hr = Fn_sw_erai_3hr/numpy.sin(numpy.deg2rad(sa))
        # get the spline interpolation function
        s = InterpolatedUnivariateSpline(erai_time_3hr, coef_3hr, k=1)
        # get the coefficient at the tower time step
        coef_tts = s(erai_time_tts)
        # get the downwelling solar radiation at the tower time step
        Fn_sw_erai_tts = coef_tts*numpy.sin(numpy.deg2rad(alt_solar_tts))
        flag = numpy.zeros(len(Fn_sw_erai_tts),dtype=numpy.int32)
        attr = qcutils.MakeAttributeDictionary(long_name="Net short wave radiation",units="W/m2")
        qcutils.CreateSeries(ds_erai,"Fn_sw",Fn_sw_erai_tts,Flag=flag,Attr=attr)
        
        Fsu_erai_tts = Fsd_erai_tts - Fn_sw_erai_tts
        flag = numpy.zeros(len(Fsu_erai_tts),dtype=numpy.int32)
        attr = qcutils.MakeAttributeDictionary(long_name="Upwelling short wave radiation",units="W/m2")
        qcutils.CreateSeries(ds_erai,"Fsu",Fsu_erai_tts,Flag=flag,Attr=attr)
        
        # Interpolate the 3 hourly accumulated downwelling longwave to the tower time step
        # NOTE: ERA-I variables are dimensioned [time,latitude,longitude]
        Fld_3d = erai_file.variables["strd"][:,:,:]
        Fld_accum = Fld_3d[:,site_lat_index,site_lon_index]
        # Downwelling longwave in ERA-I is a cummulative value that is reset to 0 at 0300 and 1500 UTC.
        # Here we convert the cummulative values to 3 hourly values.
        Fld_erai_3hr = numpy.ediff1d(Fld_accum,to_begin=0)
        idx = numpy.where((hour_utc==3)|(hour_utc==15))[0]
        Fld_erai_3hr[idx] = Fld_accum[idx]
        Fld_erai_3hr = Fld_erai_3hr/(erai_timestep*60)
        # get the spline interpolation function
        s = InterpolatedUnivariateSpline(erai_time_3hr, Fld_erai_3hr, k=1)
        # get the downwelling longwave at the tower time step
        Fld_erai_tts = s(erai_time_tts)
        flag = numpy.zeros(len(Fld_erai_tts),dtype=numpy.int32)
        attr = qcutils.MakeAttributeDictionary(long_name="Downwelling long wave radiation",units="W/m2")
        qcutils.CreateSeries(ds_erai,"Fld",Fld_erai_tts,Flag=flag,Attr=attr)
        
        # Interpolate the 3 hourly accumulated net longwave to the tower time step
        # NOTE: ERA-I variables are dimensioned [time,latitude,longitude]
        Fn_lw_3d = erai_file.variables["str"][:,:,:]
        Fn_lw_accum = Fn_lw_3d[:,site_lat_index,site_lon_index]
        # Net longwave in ERA-I is a cummulative value that is reset at 0300 and 1500 UTC.
        # Here we convert the cummulative values to 3 hourly values.
        Fn_lw_erai_3hr = numpy.ediff1d(Fn_lw_accum,to_begin=0)
        # deal with the reset times at 0300 and 1500
        idx = numpy.where((hour_utc==3)|(hour_utc==15))[0]
        Fn_lw_erai_3hr[idx] = Fn_lw_accum[idx]
        # get the average value over the 3 hourly period
        Fn_lw_erai_3hr = Fn_lw_erai_3hr/(erai_timestep*60)
        # get the spline interpolation function
        s = InterpolatedUnivariateSpline(erai_time_3hr, Fn_lw_erai_3hr, k=1)
        # get the net longwave at the tower time step
        Fn_lw_erai_tts = s(erai_time_tts)
        flag = numpy.zeros(len(Fn_lw_erai_tts),dtype=numpy.int32)
        attr = qcutils.MakeAttributeDictionary(long_name="Net long wave radiation",units="W/m2")
        qcutils.CreateSeries(ds_erai,"Fn_lw",Fn_lw_erai_tts,Flag=flag,Attr=attr)
        
        Flu_erai_tts = Fld_erai_tts - Fn_lw_erai_tts
        flag = numpy.zeros(len(Flu_erai_tts),dtype=numpy.int32)
        attr = qcutils.MakeAttributeDictionary(long_name="Upwelling long wave radiation",units="W/m2")
        qcutils.CreateSeries(ds_erai,"Flu",Flu_erai_tts,Flag=flag,Attr=attr)
        
        Fn_erai_tts = (Fsd_erai_tts - Fsu_erai_tts) + (Fld_erai_tts - Flu_erai_tts)
        flag = numpy.zeros(len(Fn_erai_tts),dtype=numpy.int32)
        attr = qcutils.MakeAttributeDictionary(long_name="Net all wave radiation",units="W/m2")
        qcutils.CreateSeries(ds_erai,"Fn",Fn_erai_tts,Flag=flag,Attr=attr)
        
        # Interpolate the 3 hourly accumulated sensible heat flux to the tower time step
        # NOTE: ERA-I variables are dimensioned [time,latitude,longitude]
        Fh_3d = erai_file.variables["sshf"][:,:,:]
        Fh_accum = float(-1)*Fh_3d[:,site_lat_index,site_lon_index]
        # Sensible heat flux in ERA-I is a cummulative value that is reset at 0300 and 1500 UTC.
        # Here we convert the cummulative values to 3 hourly values.
        Fh_erai_3hr = numpy.ediff1d(Fh_accum,to_begin=0)
        # deal with the reset times at 0300 and 1500
        idx = numpy.where((hour_utc==3)|(hour_utc==15))[0]
        Fh_erai_3hr[idx] = Fh_accum[idx]
        # get the average value over the 3 hourly period
        Fh_erai_3hr = Fh_erai_3hr/(erai_timestep*60)
        # get the spline interpolation function
        s = InterpolatedUnivariateSpline(erai_time_3hr, Fh_erai_3hr, k=1)
        # get the net longwave at the tower time step
        Fh_erai_tts = s(erai_time_tts)
        flag = numpy.zeros(len(Fh_erai_tts),dtype=numpy.int32)
        attr = qcutils.MakeAttributeDictionary(long_name="Sensible heat flux",units="W/m2")
        qcutils.CreateSeries(ds_erai,"Fh",Fh_erai_tts,Flag=flag,Attr=attr)
        
        # Interpolate the 3 hourly accumulated latent heat flux to the tower time step
        # NOTE: ERA-I variables are dimensioned [time,latitude,longitude]
        Fe_3d = erai_file.variables["slhf"][:,:,:]
        Fe_accum = float(-1)*Fe_3d[:,site_lat_index,site_lon_index]
        # Latent heat flux in ERA-I is a cummulative value that is reset at 0300 and 1500 UTC.
        # Here we convert the cummulative values to 3 hourly values.
        Fe_erai_3hr = numpy.ediff1d(Fe_accum,to_begin=0)
        # deal with the reset times at 0300 and 1500
        idx = numpy.where((hour_utc==3)|(hour_utc==15))[0]
        Fe_erai_3hr[idx] = Fe_accum[idx]
        # get the average value over the 3 hourly period
        Fe_erai_3hr = Fe_erai_3hr/(erai_timestep*60)
        # get the spline interpolation function
        s = InterpolatedUnivariateSpline(erai_time_3hr, Fe_erai_3hr, k=1)
        # get the net longwave at the tower time step
        Fe_erai_tts = s(erai_time_tts)
        flag = numpy.zeros(len(Fe_erai_tts),dtype=numpy.int32)
        attr = qcutils.MakeAttributeDictionary(long_name="Latent heat flux",units="W/m2")
        qcutils.CreateSeries(ds_erai,"Fe",Fe_erai_tts,Flag=flag,Attr=attr)
        
        # get Fg as residual
        Fg_erai_tts = Fn_erai_tts - Fh_erai_tts - Fe_erai_tts
        flag = numpy.zeros(len(Fg_erai_tts),dtype=numpy.int32)
        attr = qcutils.MakeAttributeDictionary(long_name="Ground heat flux",units="W/m2")
        qcutils.CreateSeries(ds_erai,"Fg",Fg_erai_tts,Flag=flag,Attr=attr)
        # and then Fa
        Fa_erai_tts = Fn_erai_tts - Fg_erai_tts
        flag = numpy.zeros(len(Fa_erai_tts),dtype=numpy.int32)
        attr = qcutils.MakeAttributeDictionary(long_name="Available energy",units="W/m2")
        qcutils.CreateSeries(ds_erai,"Fa",Fa_erai_tts,Flag=flag,Attr=attr)
        
        # Interpolate the 3 hourly air pressure to the tower time step
        # NOTE: ERA-I variables are dimensioned [time,latitude,longitude]
        ps_3d = erai_file.variables["sp"][:,:,:]
        ps_erai_3hr = ps_3d[:,site_lat_index,site_lon_index]/float(1000)
        # get the spline interpolation function
        s = InterpolatedUnivariateSpline(erai_time_3hr, ps_erai_3hr, k=1)
        # get the air pressure at the tower time step
        ps_erai_tts = s(erai_time_tts)
        flag = numpy.zeros(len(ps_erai_tts),dtype=numpy.int32)
        attr = qcutils.MakeAttributeDictionary(long_name="Surface pressure",units="kPa")
        qcutils.CreateSeries(ds_erai,"ps",ps_erai_tts,Flag=flag,Attr=attr)
        
        # Interpolate the 3 hourly air temperature to the tower time step
        # NOTE: ERA-I variables are dimensioned [time,latitude,longitude]
        Ta_3d = erai_file.variables["t2m"][:,:,:]
        Ta_erai_3hr = Ta_3d[:,site_lat_index,site_lon_index] - 273.15
        # get the spline interpolation function
        s = InterpolatedUnivariateSpline(erai_time_3hr, Ta_erai_3hr, k=1)
        # get the air temperature at the tower time step
        Ta_erai_tts = s(erai_time_tts)
        flag = numpy.zeros(len(Ta_erai_tts),dtype=numpy.int32)
        attr = qcutils.MakeAttributeDictionary(long_name="Air temperature",units="C")
        qcutils.CreateSeries(ds_erai,"Ta",Ta_erai_tts,Flag=flag,Attr=attr)
        
        # Interpolate the 3 hourly dew point temperature to the tower time step
        # and convert to Ah, RH and q
        # NOTE: ERA-I variables are dimensioned [time,latitude,longitude]
        Td_3d = erai_file.variables["d2m"][:,:,:]
        Td_erai_3hr = Td_3d[:,site_lat_index,site_lon_index] - 273.15
        # get the spline interpolation function
        s = InterpolatedUnivariateSpline(erai_time_3hr, Td_erai_3hr, k=1)
        # get the dew point temperature at the towespeedr time step
        Td_erai_tts = s(erai_time_tts)
        # get the relative humidity
        es_erai_tts = mf.es(Ta_erai_tts)
        e_erai_tts = mf.es(Td_erai_tts)
        VPD_erai_tts = es_erai_tts - e_erai_tts
        flag = numpy.zeros(len(VPD_erai_tts),dtype=numpy.int32)
        attr = qcutils.MakeAttributeDictionary(long_name="Vapour pressure deficit",units="kPa")
        qcutils.CreateSeries(ds_erai,"VPD",VPD_erai_tts,Flag=flag,Attr=attr)
        RH_erai_tts = float(100)*e_erai_tts/es_erai_tts
        flag = numpy.zeros(len(RH_erai_tts),dtype=numpy.int32)
        attr = qcutils.MakeAttributeDictionary(long_name="Relative humidity",units="percent")
        qcutils.CreateSeries(ds_erai,"RH",RH_erai_tts,Flag=flag,Attr=attr)
        # get the absolute humidity
        Ah_erai_tts = mf.absolutehumidityfromRH(Ta_erai_tts,RH_erai_tts)
        flag = numpy.zeros(len(Ah_erai_tts),dtype=numpy.int32)
        attr = qcutils.MakeAttributeDictionary(long_name="Absolute humidity",units="g/m3")
        qcutils.CreateSeries(ds_erai,"Ah",Ah_erai_tts,Flag=flag,Attr=attr)
        # get the specific humidity
        q_erai_tts = mf.specifichumidityfromRH(RH_erai_tts,Ta_erai_tts,ps_erai_tts)
        flag = numpy.zeros(len(q_erai_tts),dtype=numpy.int32)
        attr = qcutils.MakeAttributeDictionary(long_name="Specific humidity",units="kg/kg")
        qcutils.CreateSeries(ds_erai,"q",q_erai_tts,Flag=flag,Attr=attr)
        
        # Interpolate the 3 hourly boundary layer height to the tower time step
        # NOTE: ERA-I variables are dimensioned [time,latitude,longitude]
        Habl_3d = erai_file.variables["blh"][:,:,:]
        Habl_erai_3hr = Habl_3d[:,site_lat_index,site_lon_index]
        # get the spline interpolation function
        s = InterpolatedUnivariateSpline(erai_time_3hr, Habl_erai_3hr, k=1)
        # get the boundary layer height at the tower time step
        Habl_erai_tts = s(erai_time_tts)
        flag = numpy.zeros(len(Habl_erai_tts),dtype=numpy.int32)
        attr = qcutils.MakeAttributeDictionary(long_name="Boundary layer height",units="m")
        qcutils.CreateSeries(ds_erai,"Habl",Habl_erai_tts,Flag=flag,Attr=attr)
        
        # Spread the 3 hourly accumulated precipitation to the tower time step
        # NOTE: ERA-I variables are dimensioned [time,latitude,longitude]
        Precip_3d = erai_file.variables["tp"][:,:,:]
        Precip_accum = Precip_3d[:,site_lat_index,site_lon_index]
        Precip_erai_3hr = numpy.ediff1d(Precip_accum,to_begin=0)
        idx = numpy.where((hour_utc==3)|(hour_utc==15))[0]
        Precip_erai_3hr[idx] = Precip_accum[idx]
        Precip_erai_3hr = Precip_erai_3hr*float(1000)
        Precip_erai_tts = numpy.zeros(len(dt_erai_loc_tts))
        idx = qcutils.FindIndicesOfBInA(dt_erai_loc_cor,dt_erai_loc_tts)
        Precip_erai_tts[idx] = Precip_erai_3hr
        flag = numpy.zeros(len(Precip_erai_tts),dtype=numpy.int32)
        attr = qcutils.MakeAttributeDictionary(long_name="Precipitation",units="mm")
        qcutils.CreateSeries(ds_erai,"Precip",Precip_erai_tts,Flag=flag,Attr=attr)
        
        # Interpolate the 3 hourly soil moisture to the tower time step
        # NOTE: ERA-I variables are dimensioned [time,latitude,longitude]
        Sws_3d = erai_file.variables["swvl1"][:,:,:]
        Sws_erai_3hr = Sws_3d[:,site_lat_index,site_lon_index]
        # get the spline interpolation function
        s = InterpolatedUnivariateSpline(erai_time_3hr, Sws_erai_3hr, k=1)
        # get the soil moisture at the tower time step
        Sws_erai_tts = s(erai_time_tts)
        flag = numpy.zeros(len(Sws_erai_tts),dtype=numpy.int32)
        attr = qcutils.MakeAttributeDictionary(long_name="Soil moisture",units="frac")
        qcutils.CreateSeries(ds_erai,"Sws",Sws_erai_tts,Flag=flag,Attr=attr)
        
        # Interpolate the 3 hourly soil temperature to the tower time step
        # NOTE: ERA-I variables are dimensioned [time,latitude,longitude]
        Ts_3d = erai_file.variables["stl1"][:,:,:]
        Ts_erai_3hr = Ts_3d[:,site_lat_index,site_lon_index] - 273.15
        # get the spline interpolation function
        s = InterpolatedUnivariateSpline(erai_time_3hr, Ts_erai_3hr, k=1)
        # get the soil moisture at the tower time step
        Ts_erai_tts = s(erai_time_tts)
        flag = numpy.zeros(len(Ts_erai_tts),dtype=numpy.int32)
        attr = qcutils.MakeAttributeDictionary(long_name="Soil temperature",units="C")
        qcutils.CreateSeries(ds_erai,"Ts",Ts_erai_tts,Flag=flag,Attr=attr)
        
        # Interpolate the 3 hourly U and V components to the tower time step
        # NOTE: ERA-I variables are dimensioned [time,latitude,longitude]
        # U first ...
        U_3d = erai_file.variables["u10"][:,:,:]
        U_erai_3hr = U_3d[:,site_lat_index,site_lon_index]
        # get the spline interpolation function
        s = InterpolatedUnivariateSpline(erai_time_3hr, U_erai_3hr, k=1)
        # get the soil moisture at the tower time step
        U_erai_tts = s(erai_time_tts)
        flag = numpy.zeros(len(U_erai_tts),dtype=numpy.int32)
        attr = qcutils.MakeAttributeDictionary(long_name="U component of wind speed",units="m/s")
        qcutils.CreateSeries(ds_erai,"U",U_erai_tts,Flag=flag,Attr=attr)
        # ... then V
        V_3d = erai_file.variables["v10"][:,:,:]
        V_erai_3hr = V_3d[:,site_lat_index,site_lon_index]
        # get the spline interpolation function
        s = InterpolatedUnivariateSpline(erai_time_3hr, V_erai_3hr, k=1)
        # get the soil moisture at the tower time step
        V_erai_tts = s(erai_time_tts)
        flag = numpy.zeros(len(V_erai_tts),dtype=numpy.int32)
        attr = qcutils.MakeAttributeDictionary(long_name="V component of wind speed",units="m/s")
        qcutils.CreateSeries(ds_erai,"V",V_erai_tts,Flag=flag,Attr=attr)
        # now get the wind speed and direction
        Ws_erai_tts = numpy.sqrt(U_erai_tts*U_erai_tts + V_erai_tts*V_erai_tts)
        flag = numpy.zeros(len(Ws_erai_tts),dtype=numpy.int32)
        attr = qcutils.MakeAttributeDictionary(long_name="Wind speed",units="m/s")
        qcutils.CreateSeries(ds_erai,"Ws",Ws_erai_tts,Flag=flag,Attr=attr)
        Wd_erai_tts = float(270) - numpy.arctan2(V_erai_tts,U_erai_tts)*float(180)/numpy.pi
        idx = numpy.where(Wd_erai_tts>360)[0]
        if len(idx)>0: Wd_erai_tts[idx] = Wd_erai_tts[idx] - float(360)
        flag = numpy.zeros(len(Wd_erai_tts),dtype=numpy.int32)
        attr = qcutils.MakeAttributeDictionary(long_name="Wind direction",units="deg")
        qcutils.CreateSeries(ds_erai,"Wd",Wd_erai_tts,Flag=flag,Attr=attr)
    
        ncfile = qcio.nc_open_write(out_filename)
        qcio.nc_write_series(ncfile,ds_erai,ndims=1)