import ast
import collections
import constants as c
import datetime
import dateutil
import logging
import matplotlib.pyplot as plt
import meteorologicalfunctions as mf
import numpy
import os
import qcio
import qcrpLL
import qcrpLT
import qcrpNN
import qcts
import qcutils
import sys
import xlrd

log = logging.getLogger('qc.rp')

def CalculateET(ds):
    """
    Purpose:
     Calculate ET from Fe
    Usage:
     qcrp.CalculateET(ds)
      where ds is a data structure
    Side effects:
     Series to hold the ET data are created in ds.
    Author: PRI
    Date: June 2015
    """
    ts = int(ds.globalattributes["time_step"])
    series_list = ds.series.keys()
    Fe_list = [item for item in series_list if "Fe" in item]
    for label in Fe_list:
        suffix = label[len("Fe")+label.find("Fe"):]
        Fe,flag,attr = qcutils.GetSeriesasMA(ds,label)
        ET = Fe*ts*60/c.Lv
        attr["long_name"] = "Evapo-transpiration calculated from latent heat flux"
        attr["units"] = "mm"
        qcutils.CreateSeries(ds,"ET"+suffix,ET,Flag=flag,Attr=attr)

def CalculateNEE(cf,ds):
    """
    Purpose:
     Calculate NEE from observed Fc and observed/modeled ER.
     Input and output names are held in ds.nee.
    Usage:
     qcrp.CalculateNEE(cf,ds)
      where cf is a conbtrol file object
            ds is a data structure
    Side effects:
     Series to hold the NEE data are created in ds.
    Author: PRI
    Date: August 2014
    """
    if "nee" not in dir(ds): return
    # get the Fsd and ustar thresholds
    Fsd_threshold = float(qcutils.get_keyvaluefromcf(cf,["Params"],"Fsd_threshold",default=10))
    # get the incoming shortwave radiation and friction velocity
    Fsd,Fsd_flag,Fsd_attr = qcutils.GetSeriesasMA(ds,"Fsd")
    if "Fsd_syn" in ds.series.keys():
        Fsd_syn,flag,attr = qcutils.GetSeriesasMA(ds,"Fsd_syn")
        index = numpy.where(numpy.ma.getmaskarray(Fsd)==True)[0]
        #index = numpy.ma.where(numpy.ma.getmaskarray(Fsd)==True)[0]
        Fsd[index] = Fsd_syn[index]
    ustar,ustar_flag,ustar_attr = qcutils.GetSeriesasMA(ds,"ustar")
    for label in ds.nee.keys():
        if "Fc" not in ds.nee[label] and "ER" not in ds.nee[label]: continue
        Fc_label = ds.nee[label]["Fc"]
        ER_label = ds.nee[label]["ER"]
        output_label = ds.nee[label]["output"]
        Fc,Fc_flag,Fc_attr = qcutils.GetSeriesasMA(ds,Fc_label)
        ER,ER_flag,ER_attr = qcutils.GetSeriesasMA(ds,ER_label)
        # put the day time Fc into the NEE series
        index = numpy.ma.where(Fsd>=Fsd_threshold)[0]
        ds.series[output_label]["Data"][index] = Fc[index]
        ds.series[output_label]["Flag"][index] = Fc_flag[index]
        # put the night time ER into the NEE series
        index = numpy.ma.where(Fsd<Fsd_threshold)[0]
        ds.series[output_label]["Data"][index] = ER[index]
        ds.series[output_label]["Flag"][index] = ER_flag[index]
        # copy the attributes
        attr = ds.series[output_label]["Attr"]
        attr["units"] = Fc_attr["units"]
        attr["long_name"] = "Net Ecosystem Exchange calculated from "+Fc_label+" (Fc)"
        attr["long_name"] = attr["long_name"]+" and "+ER_label+" (ER)"
        attr["comment1"] = "Fsd threshold used was "+str(Fsd_threshold)
    del ds.nee

def CalculateNEP(cf,ds):
    """
    Purpose:
     Calculate NEP from NEE
    Usage:
     qcrp.CalculateNEP(cf,ds)
      where cf is a control file object
            ds is a data structure
    Side effects:
     Series to hold the NEP data are created in ds.
    Author: PRI
    Date: May 2015
    """
    for nee_name in cf["NEE"].keys():
        nep_name = nee_name.replace("NEE","NEP")
        nee,flag,attr = qcutils.GetSeriesasMA(ds,nee_name)
        nep = float(-1)*nee
        attr["long_name"] = "Net Ecosystem Productivity calculated as -1*"+nee_name
        qcutils.CreateSeries(ds,nep_name,nep,Flag=flag,Attr=attr)

def cleanup_ustar_dict(ldt,ustar_dict):
    """
    Purpose:
     Clean up the ustar dictionary;
      - make sure all years are included
      - fill missing year values with the mean
    Usage:
    Author: PRI
    Date: September 2015
    """
    start_year = ldt[0].year
    end_year = ldt[-1].year
    data_years = range(start_year,end_year+1)
    ustar_years = ustar_dict.keys()
    ustar_list = ustar_dict[ustar_years[0]]
    for year in data_years:
            if str(year) not in ustar_years:
                ustar_dict[str(year)] = {}
                for item in ustar_list:
                    ustar_dict[str(year)][item] = float(c.missing_value)
    # loop over the list of ustar thresholds
    year_list = ustar_dict.keys()
    year_list.sort()
    # get the average of good ustar threshold values
    good_values = []
    for year in year_list:
        ustar_threshold = float(ustar_dict[year]["ustar_mean"])
        if ustar_threshold!=float(c.missing_value):
            good_values.append(ustar_threshold)
    ustar_threshold_mean = numpy.sum(numpy.array(good_values))/len(good_values)
    # replace missing vaues with mean
    for year in year_list:
        if ustar_dict[year]["ustar_mean"]==float(c.missing_value):
            ustar_dict[year]["ustar_mean"] = ustar_threshold_mean

def ERUsingFFNET(cf,ds):
    """
    Purpose:
     Estimate ecosystem respiration using the ffnet neural network.
    Usage:
     qcrp.ERUsingFFNET(cf,ds)
      where cf is a control file object
            ds is a data structure
    Author: PRI
    Date: August 2014
    """
    if "ffnet" not in dir(ds): return
    if "ffnet" not in sys.modules.keys():
        log.error("ERUsingFFNET: I don't think ffnet is installed ...")
        return
    # local pointer to the datetime series
    ldt = ds.series["DateTime"]["Data"]
    startdate = ldt[0]
    enddate = ldt[-1]
    FFNET_info = {"file_startdate":startdate.strftime("%Y-%m-%d %H:%M"),
                    "file_enddate":enddate.strftime("%Y-%m-%d %H:%M"),
                    "plot_path":cf["Files"]["plot_path"]}
    # check to see if this is a batch or an interactive run
    call_mode = qcutils.get_keyvaluefromcf(cf,["Options"],"call_mode",default="interactive")
    FFNET_info["call_mode"]= call_mode
    if call_mode.lower()=="interactive":
        FFNET_info["show_plots"] = True
        # call the FFNET GUI
        qcrpNN.rpFFNET_gui(cf,ds,FFNET_info)
    else:
        if "GUI" in cf:
            if "FFNET" in cf["GUI"]:
                qcrpNN.rpFFNET_run_nogui(cf,ds,FFNET_info)

def ERUsingLasslop(cf,ds):
    if "rpLL" not in dir(ds): return
    log.info("Estimating ER using Lasslop")
    # these should be read from the control file
    series = ds.rpLL.keys()
    info = {"window_length":int(ds.rpLL[series[0]]["window_size_days"]),
            "window_offset":int(ds.rpLL[series[0]]["step_size_days"]),
            "fsd_threshold":10}
    ldt = ds.series["DateTime"]["Data"]
    ts = int(ds.globalattributes["time_step"])
    info["ts"] = ts
    site_name = ds.globalattributes["site_name"]
    nrecs = int(ds.globalattributes["nc_nrecs"])
    # get the data and synchronise the gaps
    # *** PUT INTO SEPARATE FUNCTION
    indicator = numpy.ones(nrecs,dtype=numpy.int)
    Fsd,f,a = qcutils.GetSeriesasMA(ds,"Fsd")
    idx = numpy.where(f!=0)[0]
    indicator[idx] = numpy.int(0)
    D,f,a = qcutils.GetSeriesasMA(ds,"VPD")
    idx = numpy.where(f!=0)[0]
    indicator[idx] = numpy.int(0)
    T,f,a = qcutils.GetSeriesasMA(ds,"Ta")
    idx = numpy.where(f!=0)[0]
    indicator[idx] = numpy.int(0)
    ustar,f,a = qcutils.GetSeriesasMA(ds,"ustar")
    idx = numpy.where(f!=0)[0]
    indicator[idx] = numpy.int(0)
    Fc,f,Fc_attr = qcutils.GetSeriesasMA(ds,"Fc")
    idx = numpy.where(f!=0)[0]
    indicator[idx] = numpy.int(0)
    indicator_night = numpy.copy(indicator)
    # ***
    # apply a day/night filter
    idx = numpy.where(Fsd>=10)[0]
    indicator_night[idx] = numpy.int(0)
    # replace this with a check to see if a turbulence filter has been applied
    # apply a simple ustar filter
    #idx = numpy.where(ustar<=0.24)[0]
    #indicator_night[idx] = numpy.int(0)
    # synchronise the gaps and apply the ustar filter
    T_night = numpy.ma.masked_where(indicator_night==0,T)
    ustar_night = numpy.ma.masked_where(indicator_night==0,ustar)
    ER = numpy.ma.masked_where(indicator_night==0,Fc)
    # loop over the windows and get E0
    log.info("Estimating the rb and E0 parameters")
    LT_results = qcrpLL.get_LT_params(ldt,ER,T_night,info)
    # interpolate parameters
    # this should have a check to make sure we are not interpolating with a small
    # number of points
    LT_results["rb_int"] = qcrpLL.interp_params(LT_results["rb"])
    LT_results["E0_int"] = qcrpLL.interp_params(LT_results["E0"])
    # get series of rb and E0 from LT at the tower stime step
    # *** PUT INTO SEPARATE FUNCTION
    ntsperday = float(24)*float(60)/float(ts)
    days_at_beginning = float(info["window_length"])/2 - float(info["window_offset"])/2
    rb_beginning = numpy.ones(days_at_beginning*ntsperday)*LT_results["rb_int"][0]
    rb_middle = numpy.repeat(LT_results["rb_int"],info["window_offset"]*ntsperday)
    nend = len(ldt) - (len(rb_beginning)+len(rb_middle))
    rb_end = numpy.ones(nend)*LT_results["rb_int"][-1]
    rb_tts = numpy.concatenate((rb_beginning,rb_middle,rb_end))
    E0_beginning = numpy.ones(days_at_beginning*ntsperday)*LT_results["E0_int"][0]
    E0_middle = numpy.repeat(LT_results["E0_int"],info["window_offset"]*ntsperday)
    nend = len(ldt) - (len(E0_beginning)+len(E0_middle))
    E0_end = numpy.ones(nend)*LT_results["E0_int"][-1]
    E0_tts = numpy.concatenate((E0_beginning,E0_middle,E0_end))
    # ***
    # and get the ecosystem respiration at the tower time step
    log.info("Calculating ER using Lloyd-Taylor")
    ER_LT = qcrpLL.ER_LloydTaylor(T,rb_tts,E0_tts)
    # plot the L&T parameters and ER_LT
    #qcrpLL.plot_LTparams_ER(ldt,ER,ER_LT,LT_results)
    # get a day time indicator
    indicator_day = numpy.copy(indicator)
    # apply a day/night filter
    idx = numpy.where(Fsd<=info["fsd_threshold"])[0]
    indicator_day[idx] = numpy.int(0)
    # synchronise the gaps and apply the day/night filter
    Fsd_day = numpy.ma.masked_where(indicator_day==0,Fsd)
    D_day = numpy.ma.masked_where(indicator_day==0,D)
    T_day = numpy.ma.masked_where(indicator_day==0,T)
    NEE_day = numpy.ma.masked_where(indicator_day==0,Fc)
    # get the Lasslop parameters
    log.info("Estimating the Lasslop parameters")
    LL_results = qcrpLL.get_LL_params(ldt,Fsd_day,D_day,T_day,NEE_day,ER,LT_results,info)
    # interpolate parameters
    LL_results["alpha_int"] = qcrpLL.interp_params(LL_results["alpha"])
    LL_results["beta_int"] = qcrpLL.interp_params(LL_results["beta"])
    LL_results["k_int"] = qcrpLL.interp_params(LL_results["k"])
    LL_results["rb_int"] = qcrpLL.interp_params(LL_results["rb"])
    LL_results["E0_int"] = qcrpLL.interp_params(LL_results["E0"])
    #qcrpLL.plot_LLparams(LT_results,LL_results)
    # get the Lasslop parameters at the tower time step
    # *** PUT INTO SEPARATE FUNCTION
    ntsperday = float(24)*float(60)/float(ts)
    days_at_beginning = float(info["window_length"])/2 - float(info["window_offset"])/2
    int_list = ["alpha_int","beta_int","k_int","rb_int","E0_int"]
    tts_list = ["alpha_tts","beta_tts","k_tts","rb_tts","E0_tts"]
    for tts_item,int_item in zip(tts_list,int_list):
        beginning = numpy.ones(days_at_beginning*ntsperday)*LL_results[int_item][0]
        middle = numpy.repeat(LL_results[int_item],info["window_offset"]*ntsperday)
        nend = len(ldt) - (len(beginning)+len(middle))
        end = numpy.ones(nend)*LL_results[int_item][-1]
        LL_results[tts_item] = numpy.concatenate((beginning,middle,end))
    # ***
    # get ER, GPP and NEE using Lasslop
    D0 = LL_results["D0"]
    rb = LL_results["rb_tts"]
    units = "umol/m2/s"
    long_name = "Base respiration at Tref from Lloyd-Taylor method used in Lasslop et al (2010)"
    attr = qcutils.MakeAttributeDictionary(long_name=long_name,units=units)
    flag = numpy.zeros(len(rb),dtype=numpy.int32)
    qcutils.CreateSeries(ds,"rb_LL",rb,Flag=flag,Attr=attr)
    E0 = LL_results["E0_tts"]
    units = "C"
    long_name = "Activation energy from Lloyd-Taylor method used in Lasslop et al (2010)"
    attr = qcutils.MakeAttributeDictionary(long_name=long_name,units=units)
    qcutils.CreateSeries(ds,"E0_LL",E0,Flag=flag,Attr=attr)
    log.info("Calculating ER using Lloyd-Taylor with Lasslop parameters")
    ER_LL = qcrpLL.ER_LloydTaylor(T,rb,E0)
    # write ecosystem respiration modelled by Lasslop et al (2010)
    units = Fc_attr["units"]
    long_name = "Ecosystem respiration modelled by Lasslop et al (2010)"
    attr = qcutils.MakeAttributeDictionary(long_name=long_name,units=units)
    qcutils.CreateSeries(ds,"ER_LL_all",ER_LL,Flag=flag,Attr=attr)
    # parameters associated with GPP and GPP itself
    alpha = LL_results["alpha_tts"]
    units = "umol/J"
    long_name = "Canopy light use efficiency"
    attr = qcutils.MakeAttributeDictionary(long_name=long_name,units=units)
    qcutils.CreateSeries(ds,"alpha_LL",alpha,Flag=flag,Attr=attr)
    beta = LL_results["beta_tts"]
    units = "umol/m2/s"
    long_name = "Maximum CO2 uptake at light saturation"
    attr = qcutils.MakeAttributeDictionary(long_name=long_name,units=units)
    qcutils.CreateSeries(ds,"beta_LL",beta,Flag=flag,Attr=attr)
    k = LL_results["k_tts"]
    units = "none"
    long_name = "Sensitivity of response to VPD"
    attr = qcutils.MakeAttributeDictionary(long_name=long_name,units=units)
    qcutils.CreateSeries(ds,"k_LL",k,Flag=flag,Attr=attr)
    GPP_LL = qcrpLL.GPP_RHLRC_D(Fsd,D,alpha,beta,k,D0)
    units = "umol/m2/s"
    long_name = "GPP modelled by Lasslop et al (2010)"
    attr = qcutils.MakeAttributeDictionary(long_name=long_name,units=units)
    qcutils.CreateSeries(ds,"GPP_LL_all",GPP_LL,Flag=flag,Attr=attr)
    # NEE
    data = {"Fsd":Fsd,"T":T,"D":D}
    NEE_LL = qcrpLL.NEE_RHLRC_D(data,alpha,beta,k,D0,rb,E0)
    units = "umol/m2/s"
    long_name = "NEE modelled by Lasslop et al (2010)"
    attr = qcutils.MakeAttributeDictionary(long_name=long_name,units=units)
    qcutils.CreateSeries(ds,"NEE_LL_all",NEE_LL,Flag=flag,Attr=attr)

def ERUsingLloydTaylor(cf,ds):
    """
    Purpose:
     Estimate ecosystem respiration using Lloyd-Taylor.
     Ian McHugh wrote the LT code, PRI wrote the wrapper to integrate
     this with OzFluxQC.
    Usage:
    Author: IMcH, PRI
    Date: October 2015
    """
    if "rpLT" not in dir(ds): return
    log.info(' Estimating ER using Lloyd-Taylor')
    long_name = "Ecosystem respiration modelled by Lloyd-Taylor"
    ER_attr = qcutils.MakeAttributeDictionary(long_name=long_name,units="umol/m2/s")
    ts = int(ds.globalattributes["time_step"])
    site_name = ds.globalattributes["site_name"]
    ldt = ds.series["DateTime"]["Data"]
    startdate = ldt[0]
    enddate = ldt[-1]
    nperhr = int(float(60)/ts+0.5)
    nperday = int(float(24)*nperhr+0.5)
    LT_info = {"file_startdate":startdate.strftime("%Y-%m-%d %H:%M"),
               "file_enddate":enddate.strftime("%Y-%m-%d %H:%M"),
               "plot_path":cf["Files"]["plot_path"],
               "show_plots":False,"time_step":ts,"nperday":nperday}
    call_mode = qcutils.get_keyvaluefromcf(cf,["Options"],"call_mode",default="interactive")
    LT_info["call_mode"]= call_mode
    if call_mode.lower()=="interactive": LT_info["show_plots"] = True
    # set the figure number
    if len(plt.get_fignums())==0:
        fig_num = 0
    else:
        fig_num = plt.get_fignums()[-1]
    # open the Excel file
    nc_name = qcio.get_outfilenamefromcf(cf)
    xl_name = nc_name.replace(".nc","_L&T.xls")
    xl_file = qcio.xl_open_write(xl_name)
    if xl_file=='':
        log.error("ERUsingLloydTaylor: error opening Excel file "+xl_name)
        return
    # loop over the series
    for series in ds.rpLT.keys():
        # create dictionaries for the results
        E0_results = {}
        E0_raw_results = {}
        rb_results = {}
        # add a sheet for this series
        xl_sheet = xl_file.add_sheet(series)
        # get a local copy of the config dictionary
        configs_dict = ds.rpLT[series]["configs_dict"]
        configs_dict["measurement_interval"] = float(ts)/60.0
        data_dict = qcrpLT.get_data_dict(ds,configs_dict)
        # *** start of code taken from Ian McHugh's Partition_NEE.main ***
        # If user wants individual window plots, check whether output directories
        # are present, and create if not
        if configs_dict['output_plots']:
            output_path = configs_dict['output_path']
            configs_dict['window_plot_output_path'] = output_path
            if not os.path.isdir(output_path): os.makedirs(output_path)
        # Get arrays of all datetimes, all dates and stepped dates original code
        datetime_array = data_dict.pop('date_time')
        (step_date_index_dict,
         all_date_index_dict,
         year_index_dict) = qcrpLT.get_dates(datetime_array, configs_dict)
        date_array = numpy.array(all_date_index_dict.keys())
        date_array.sort()
        step_date_array = numpy.array(step_date_index_dict.keys())
        step_date_array.sort()
        # Create variable name lists for results output
        series_rslt_list = ['Nocturnally derived Re', 'GPP from nocturnal derived Re',
                            'Daytime derived Re', 'GPP from daytime derived Re']
        new_param_list = ['Eo', 'rb_noct', 'rb_day', 'alpha_fixed_rb',
                          'alpha_free_rb', 'beta_fixed_rb', 'beta_free_rb',
                          'k_fixed_rb', 'k_free_rb', 'Eo error code',
                          'Nocturnal rb error code',
                          'Light response parameters + fixed rb error code',
                          'Light response parameters + free rb error code']
        # Create dictionaries for results
        # First the parameter estimates and error codes...
        empty_array = numpy.empty([len(date_array)])
        empty_array[:] = numpy.nan
        opt_params_dict = {var: empty_array.copy() for var in new_param_list}
        opt_params_dict['date'] = date_array
        # Then the time series estimation
        empty_array = numpy.empty([len(datetime_array)])
        empty_array[:] = numpy.nan
        series_est_dict = {var: empty_array.copy() for var in series_rslt_list}
        series_est_dict['date_time'] = datetime_array
        # Create a dictionary containing initial guesses for each parameter
        params_dict = qcrpLT.make_initial_guess_dict(data_dict)    
        # *** start of annual estimates of E0 code ***
        # this section could be a separate routine
        # Get the annual estimates of Eo
        log.info(" Optimising fit for Eo for each year")
        Eo_dict, EoQC_dict, Eo_raw_dict, EoQC_raw_dict = qcrpLT.optimise_annual_Eo(data_dict,params_dict,configs_dict,year_index_dict)
        #print 'Done!'
        # Write to result arrays
        year_array = numpy.array([i.year for i in date_array])
        for yr in year_array:
            index = numpy.where(year_array == yr)
            opt_params_dict['Eo'][index] = Eo_dict[yr]
            opt_params_dict['Eo error code'][index] = EoQC_dict[yr]
        E0_results["DateTime"] = {"data":[datetime.datetime(int(yr),1,1) for yr in Eo_dict.keys()],
                                  "units":"Year","format":"yyyy"}
        E0_results["E0"] = {"data":[float(Eo_dict[yr]) for yr in Eo_dict.keys()],
                            "units":"none","format":"0"}
        E0_raw_results["DateTime"] = {"data":[datetime.datetime(int(yr),1,1) for yr in Eo_raw_dict.keys()],
                                  "units":"Year","format":"yyyy"}
        E0_raw_results["E0"] = {"data":[float(Eo_raw_dict[yr]) for yr in Eo_raw_dict.keys()],
                            "units":"none","format":"0"}
        # write the E0 values to the Excel file
        qcio.xl_write_data(xl_sheet,E0_raw_results,xlCol=0)
        qcio.xl_write_data(xl_sheet,E0_results,xlCol=2)
        # *** end of annual estimates of E0 code ***
        # *** start of estimating rb code for each window ***
        # this section could be a separate routine
        # Rewrite the parameters dictionary so that there will be one set of 
        # defaults for the free and one set of defaults for the fixed parameters
        params_dict = {'fixed_rb': qcrpLT.make_initial_guess_dict(data_dict),
                       'free_rb': qcrpLT.make_initial_guess_dict(data_dict)}
        # Do nocturnal optimisation for each window
        log.info(" Optimising fit for rb using nocturnal data")
        for date in step_date_array:
            # Get Eo for the relevant year and write to the parameters dictionary
            param_index = numpy.where(date_array == date)
            params_dict['fixed_rb']['Eo_default'] = opt_params_dict['Eo'][param_index]
            # Subset the data and check length
            sub_dict = qcrpLT.subset_window(data_dict, step_date_index_dict[date])
            noct_dict = qcrpLT.subset_window(data_dict, step_date_index_dict[date])
            # Subset again to remove daytime and then nan
            #noct_dict = qcrpLT.subset_daynight(sub_dict, noct_flag = True)
            len_all_noct = len(noct_dict['NEE'])
            noct_dict = qcrpLT.subset_nan(noct_dict)
            len_valid_noct = len(noct_dict['NEE'])
            # Do optimisation only if data passes minimum threshold
            if round(float(len_valid_noct) / len_all_noct * 100) > \
            configs_dict['minimum_pct_noct_window']:
                params, error_state = qcrpLT.optimise_rb(noct_dict,params_dict['fixed_rb'])
            else:
                params, error_state = [numpy.nan], 10                                                      
            # Send data to the results dict
            opt_params_dict['rb_noct'][param_index] = params
            opt_params_dict['Nocturnal rb error code'][param_index] = error_state
            # Estimate time series and plot if requested
            if error_state == 0 and configs_dict['output_plots']:
                this_params_dict = {'Eo': opt_params_dict['Eo'][param_index],
                                    'rb': opt_params_dict['rb_noct'][param_index]}
                est_series_dict = qcrpLT.estimate_Re_GPP(sub_dict, this_params_dict)
                combine_dict = dict(sub_dict, **est_series_dict)
                qcrpLT.plot_windows(combine_dict, configs_dict, date, noct_flag = True)
        # get a copy of the rb data before interpolation so we can write it to file
        rb_date = opt_params_dict["date"]
        rb_data = opt_params_dict["rb_noct"]
        # get the indices of non-NaN elements
        idx = numpy.where(numpy.isnan(rb_data)!=True)[0]
        # get the datetime dictionary
        rb_results["DateTime"] = {"data":rb_date[idx],
                                  "units":"Date","format":"dd/mm/yyyy"}
        # get the rb values
        rb_results["rb_noct"] = {"data":rb_data[idx],
                            "units":"none","format":"0.00"}
        # write to the Excel file
        qcio.xl_write_data(xl_sheet,rb_results,xlCol=4)
        # Interpolate
        opt_params_dict['rb_noct'] = qcrpLT.interp_params(opt_params_dict['rb_noct'])
        #print 'Done!'
        # *** end of estimating rb code for each window ***
        # *** start of code to calculate ER from fit parameters
        # this section could be a separate routine
        E0 = numpy.zeros(len(ldt))
        rb = numpy.zeros(len(ldt))
        ldt_year = numpy.array([dt.year for dt in ldt])
        ldt_month = numpy.array([dt.month for dt in ldt])
        ldt_day = numpy.array([dt.day for dt in ldt])
        for date,E0_val,rb_val in zip(opt_params_dict["date"],opt_params_dict["Eo"],opt_params_dict["rb_noct"]):
            param_year = date.year
            param_month = date.month
            param_day = date.day
            idx = numpy.where((ldt_year==param_year)&(ldt_month==param_month)&(ldt_day==param_day))[0]
            E0[idx] = E0_val
            rb[idx] = rb_val
        T_label = configs_dict["drivers"]
        T,T_flag,a = qcutils.GetSeriesasMA(ds,T_label)
        ER_LT = qcrpLT.TRF(data_dict, E0, rb)
        ER_LT_flag = numpy.empty(len(ER_LT),dtype=numpy.int32)
        ER_LT_flag.fill(30)
        #ER_LT = qcrpLT.ER_LloydTaylor(T,E0,rb)
        target = str(ds.rpLT[series]["target"])
        #drivers = str(configs_dict["drivers"])
        drivers = ds.rpLT[series]["drivers"]
        output = str(configs_dict["output_label"])
        ER_attr["comment1"] = "Drivers were "+str(drivers)
        qcutils.CreateSeries(ds,output,ER_LT,Flag=ER_LT_flag,Attr=ER_attr)
        # plot the respiration estimated using Lloyd-Taylor
        fig_num = fig_num + 1
        title = site_name+" : "+series+" estimated using Lloyd-Taylor"
        pd = qcrpLT.rpLT_initplot(site_name=site_name,label=target,fig_num=fig_num,title=title,
                             nDrivers=len(drivers),startdate=str(startdate),enddate=str(enddate))
        qcrpLT.rpLT_plot(pd,ds,series,drivers,target,output,LT_info)
    # close the Excel workbook
    xl_file.save(xl_name)

def ERUsingSOLO(cf,ds):
    """ Estimate ER using SOLO. """
    if "solo" not in dir(ds): return
    # local pointer to the datetime series
    ldt = ds.series["DateTime"]["Data"]
    startdate = ldt[0]
    enddate = ldt[-1]
    solo_info = {"file_startdate":startdate.strftime("%Y-%m-%d %H:%M"),
                   "file_enddate":enddate.strftime("%Y-%m-%d %H:%M"),
                   "plot_path":cf["Files"]["plot_path"]}
    # check to see if this is a batch or an interactive run
    call_mode = qcutils.get_keyvaluefromcf(cf,["Options"],"call_mode",default="interactive")
    solo_info["call_mode"]= call_mode
    if call_mode.lower()=="interactive": solo_info["show_plots"] = True
    if call_mode.lower()=="interactive":
        # call the ERUsingSOLO GUI
        qcrpNN.rpSOLO_gui(cf,ds,solo_info)
    else:
        if "GUI" in cf:
            if "SOLO" in cf["GUI"]:
                qcrpNN.rpSOLO_run_nogui(cf,ds,solo_info)

#def GetERFromFc(cf,ds):
    #"""
    #Purpose:
     #Get the observed ecosystem respiration from measurements of Fc by
     #filtering out daytime periods and periods when ustar is less than
     #a threshold value.
     #The Fsd threshold for determining day time and night time and the
     #ustar threshold are set in the [Params] section of the L5 control
     #file.
    #Usage:
     #qcrp.GetERFromFc(cf,ds)
     #where cf is a control file object
           #ds is a data structure
    #Side effects:
     #A new series called "ER" is created in the data structure.
    #Author: PRI
    #Date: August 2014
    #"""
    ## needs a fecking good refactor
    #ts = int(ds.globalattributes["time_step"])
    #ldt = ds.series['DateTime']['Data']
    ## get the Fsd threshold
    #if "Options" in cf.keys():
        #if "Fsd_threshold" in cf["Options"].keys():
            #Fsd_threshold = float(cf["Options"]["Fsd_threshold"])
        #else:
            #log.warning(" No Fsd threshold in [Options] section of control file")
            #log.warning(" ... using default value of 10 W/m2")
            #Fsd_threshold = float(10)
    #else:
        #log.warning(" No [Options] section of control file for Fsd threshold")
        #log.warning(" ... using default value of 10 W/m2")
        #Fsd_threshold = float(10)
    ## get the ustar thresholds
    #if "cpd_filename" in cf["Files"]:
        #ustar_dict = get_ustarthreshold_from_cpdresults(cf)
    #else:
        #msg = " CPD results filename not in control file"
        #log.warning(msg)
        #ustar_dict = get_ustarthreshold_from_cf(cf,ldt)
    ## make sure we have an entry in ustar_dict for all years
    #start_year = ldt[0].year
    #end_year = ldt[-1].year
    #data_years = range(start_year,end_year+1)
    #ustar_years = ustar_dict.keys()
    #ustar_list = ustar_dict[ustar_years[0]]
    #for year in data_years:
        #if str(year) not in ustar_years:
            #ustar_dict[str(year)] = {}
            #for item in ustar_list:
                #ustar_dict[str(year)][item] = float(c.missing_value)

    ## get the data
    #Fsd,Fsd_flag,Fsd_attr = qcutils.GetSeriesasMA(ds,"Fsd")
    #if "solar_altitude" not in ds.series.keys(): qcts.get_synthetic_fsd(ds)
    #sa,flag,attr = qcutils.GetSeriesasMA(ds,"solar_altitude")
    #ustar,ustar_flag,attr = qcutils.GetSeriesasMA(ds,"ustar")
    #Fc,Fc_flag,Fc_attr = qcutils.GetSeriesasMA(ds,"Fc")
    ## get a copy of the Fc flag
    #ER_flag = numpy.array(Fc_flag)

    ## only accept Fc and ustar data when both have a QC flag value of 0
    #ustar = numpy.ma.masked_where((ustar_flag!=0)|(Fc_flag!=0),ustar)
    #Fc = numpy.ma.masked_where((ustar_flag!=0)|(Fc_flag!=0),Fc)
    #index_notok = numpy.where((ustar_flag!=0)|(Fc_flag!=0))[0]
    ##ustar_flag[index_notok] = numpy.int32(61)
    #ER_flag[index_notok] = numpy.int32(61)
    ## check for any missing data
    #for item,label in zip([Fsd],["Fsd"]):
        #index = numpy.where(numpy.ma.getmaskarray(item)==True)[0]
        #if len(index)!=0:
            #log.error(" GetERFromFc: missing data in series "+label)
            #raise Exception("GetERFromFc: missing data in series "+label)

    ## apply the day/night filter
    ## get the day/night filter type from the control file
    #daynightfilter_type = qcutils.get_keyvaluefromcf(cf,["Options"],"DayNightFilter",default="Fsd")
    ## trap any types not implemented and set to Fsd
    #if daynightfilter_type not in ["Fsd","sa"]: daynightfilter_type = "Fsd"
    ## make the attribute dictionary first so we can add the ustar thresholds to it
    #ER_attr = qcutils.MakeAttributeDictionary(long_name='Ecosystem respiration (observed)',units=Fc_attr["units"])

    ## apply the day/night filter
    #if daynightfilter_type=="Fsd":
        ## we are using Fsd and possibly Fsd_syn to define day/night
        #ER_attr["Fsd_threshold"] = str(Fsd_threshold)
        ## we are only using Fsd
        #ER1 = numpy.ma.masked_where(Fsd>Fsd_threshold,Fc,copy=True)
        #index_daynight = numpy.ma.where(Fsd>Fsd_threshold)[0]
        #ER_flag[index_daynight] = numpy.int32(62)
    #else:
        #sa_threshold = int(qcutils.get_keyvaluefromcf(cf,["Options"],"sa_threshold",default="-5"))
        #ER_attr["sa_threshold"] = str(sa_threshold)
        #ER1 = numpy.ma.masked_where(sa>sa_threshold,Fc,copy=True)
        #index_daynight = numpy.ma.where(sa>sa_threshold)[0]
        #ER_flag[index_daynight] = numpy.int32(63)
    ## get a copy of the day/night filtered data
    #ER2 = numpy.ma.array(ER1)

    ## loop over the list of ustar thresholds
    #year_list = ustar_dict.keys()
    #year_list.sort()
    ## get the average of good ustar threshold values
    #good_values = []
    #for year in year_list:
        #ustar_threshold = float(ustar_dict[year]["ustar_mean"])
        #if ustar_threshold!=float(c.missing_value):
            #good_values.append(ustar_threshold)
    #ustar_threshold_mean = numpy.sum(numpy.array(good_values))/len(good_values)
    ## now loop over the years in the data to apply the ustar threshold
    #for year in year_list:
        #start_date = str(year)+"-01-01 00:30"
        #if ts==60: start_date = str(year)+"-01-01 01:00"
        #end_date = str(int(year)+1)+"-01-01 00:00"
        ## get the ustar threshold
        #ustar_threshold = float(ustar_dict[year]["ustar_mean"])
        #if ustar_threshold==float(c.missing_value): ustar_threshold = ustar_threshold_mean
        #ER_attr["ustar_threshold_"+str(year)] = str(ustar_threshold)
        ## get the start and end datetime indices
        #si = qcutils.GetDateIndex(ldt,start_date,ts=ts,default=0,match='exact')
        #ei = qcutils.GetDateIndex(ldt,end_date,ts=ts,default=len(ldt),match='exact')
        ## filter out the low ustar conditions
        #ER2[si:ei] = numpy.ma.masked_where(ustar[si:ei]<ustar_threshold,ER1[si:ei])
        ## set the QC flag
        #index_lowustar = numpy.ma.where(ustar[si:ei]<ustar_threshold)[0]
        #ER_flag[si:ei][index_lowustar] = numpy.int32(64)

    ## apply quantile filter
    #if qcutils.cfoptionskeylogical(cf,Key='UseQuantileFilter',default=False):
        #ER_attr["long_name"] = ER_attr["long_name"]+", quantile filter not used"
        #qcutils.CreateSeries(ds,"ER_nqf",ER2,Flag=ER_flag,Attr=ER_attr)
        #quantile_lower = float(qcutils.get_keyvaluefromcf(cf,["Options"],"QuantileValue",default="2.5"))
        #quantile_upper = float(100) - quantile_lower
        #q = numpy.percentile(numpy.ma.compressed(ER2),[quantile_lower,quantile_upper])
        #ER2 = numpy.ma.masked_where((ER2<q[0])|(ER2>q[1]),ER2)
        #index_qf = numpy.ma.where((ER2<q[0])|(ER2>q[1]))[0]
        #ER_flag[index_qf] = numpy.int32(65)
        #ER_attr["long_name"].replace(", quantile filter not used",", quantile filter used")
        #ER_attr["ER_quantile"] = str(quantile_lower)+","+str(quantile_upper)

    ## put the nocturnal, filtered Fc data into the data structure
    #qcutils.CreateSeries(ds,"ER",ER2,Flag=ER_flag,Attr=ER_attr)
    #return

#def GetERFromFc(cf,ds):
    #"""
    #Purpose:
     #Get the observed ecosystem respiration from measurements of Fc by
     #filtering out daytime periods and periods when ustar is less than
     #a threshold value.
     #The Fsd threshold for determining day time and night time and the
     #ustar threshold are set in the [Params] section of the L5 control
     #file.
     #Re-write of the original penned in August 2014
    #Usage:
     #qcrp.GetERFromFc(cf,ds)
     #where cf is a control file object
           #ds is a data structure
    #Side effects:
     #A new series called "ER" is created in the data structure.
    #Author: PRI
    #Date: October 2015
    #"""
    #ldt = ds.series["DateTime"]["Data"]
    #ts = int(ds.globalattributes["time_step"])
    ## get the ustar thresholds
    #ustar_dict = get_ustar_thresholds(cf,ldt)
    ## get the data
    #Fsd,Fsd_flag,Fsd_attr = qcutils.GetSeriesasMA(ds,"Fsd")
    #if "solar_altitude" not in ds.series.keys(): qcts.get_synthetic_fsd(ds)
    #Fsd_syn,flag,attr = qcutils.GetSeriesasMA(ds,"Fsd_syn")
    #sa,flag,attr = qcutils.GetSeriesasMA(ds,"solar_altitude")
    #ustar,ustar_flag,ustar_attr = qcutils.GetSeriesasMA(ds,"ustar")
    #Fc,Fc_flag,Fc_attr = qcutils.GetSeriesasMA(ds,"Fc")
    ## get the Monin-Obukhov length
    #Ta,flag,attr = qcutils.GetSeriesasMA(ds,"Ta")
    #Ah,flag,attr = qcutils.GetSeriesasMA(ds,"Ah")
    #ps,flag,attr = qcutils.GetSeriesasMA(ds,"ps")
    #Fh,flag,Fh_attr = qcutils.GetSeriesasMA(ds,"Fh")
    #L = mf.molen(Ta,Ah,ps,ustar,Fh,fluxtype="sensible")
    ## get a copy of the Fc flag and make the attribute dictionary
    #ER_flag = numpy.array(Fc_flag)
    #long_name = "Ecosystem respiration (observed)"
    #units = Fc_attr["units"]
    #ER_attr = qcutils.MakeAttributeDictionary(long_name=long_name,units=units)
    ## check for any missing data
    #series_list = [Fsd,sa,ustar,Fc]
    #label_list = ["Fsd","sa","ustar","Fc"]
    #result = check_for_missing_data(series_list,label_list)
    #if result!=1: return 0
    ## only accept Fc and ustar data when both have a QC flag value of 0
    #ustar = numpy.ma.masked_where((ustar_flag!=0)|(Fc_flag!=0),ustar)
    #Fc = numpy.ma.masked_where((ustar_flag!=0)|(Fc_flag!=0),Fc)
    #index_notok = numpy.where((ustar_flag!=0)|(Fc_flag!=0))[0]
    #ER_flag[index_notok] = numpy.int32(61)
    ## get the indicator series
    #turbulence_indicator = get_turbulence_indicator(cf,ldt,ustar,L,ustar_dict,ts,ER_attr)
    #idx = numpy.where(turbulence_indicator==0)[0]
    #ER_flag[idx] = numpy.int32(64)
    #daynight_indicator = get_daynight_indicator(cf,Fsd,Fsd_syn,sa,ER_attr)
    #idx = numpy.where(daynight_indicator==0)[0]
    #ER_flag[idx] = numpy.int32(63)
    #er_indicator = turbulence_indicator*daynight_indicator
    ## apply the filter to get ER from Fc
    #ER = numpy.ma.masked_where(er_indicator==0,Fc,copy=True)
    #qcutils.CreateSeries(ds,"ER",ER,Flag=ER_flag,Attr=ER_attr)
    #return 1

def GetERFromFc2(cf,ds):
    """
    Purpose:
     Get the observed ecosystem respiration from measurements of Fc by
     filtering out daytime periods.  Note that the removal of low tubulence
     periods has been done by qcck.ApplyTurbulenceFilter() before this
     routine is called.
     The Fsd threshold for determining day time and night time and the
     ustar threshold are set in the [Params] section of the L5 control
     file.
     Re-write of the original penned in August 2014
    Usage:
     qcrp.GetERFromFc(cf,ds)
     where cf is a control file object
           ds is a data structure
    Side effects:
     A new series called "ER" is created in the data structure.
    Author: PRI
    Date: October 2015
    """
    ldt = ds.series["DateTime"]["Data"]
    ts = int(ds.globalattributes["time_step"])
    # get the data
    Fsd,Fsd_flag,Fsd_attr = qcutils.GetSeriesasMA(ds,"Fsd")
    if "solar_altitude" not in ds.series.keys(): qcts.get_synthetic_fsd(ds)
    Fsd_syn,flag,attr = qcutils.GetSeriesasMA(ds,"Fsd_syn")
    sa,flag,attr = qcutils.GetSeriesasMA(ds,"solar_altitude")
    Fc,Fc_flag,Fc_attr = qcutils.GetSeriesasMA(ds,"Fc")
    # get a copy of the Fc flag and make the attribute dictionary
    ER_flag = numpy.array(Fc_flag)
    long_name = "Ecosystem respiration (observed)"
    units = Fc_attr["units"]
    ER_attr = qcutils.MakeAttributeDictionary(long_name=long_name,units=units)
    ## check for any missing data
    #series_list = [Fsd,sa,Fc]
    #label_list = ["Fsd","sa","Fc"]
    #result = check_for_missing_data(series_list,label_list)
    #if result!=1: return 0
    # only accept Fc with QC flag value of 0
    Fc = numpy.ma.masked_where((Fc_flag!=0),Fc)
    index_notok = numpy.where((Fc_flag!=0))[0]
    ER_flag[index_notok] = numpy.int32(61)
    # get the indicator series
    daynight_indicator = get_daynight_indicator(cf,Fsd,Fsd_syn,sa)
    idx = numpy.where(daynight_indicator["values"]==0)[0]
    ER_flag[idx] = numpy.int32(63)
    # apply the filter to get ER from Fc
    ER = numpy.ma.masked_where(daynight_indicator["values"]==0,Fc,copy=True)
    for item in daynight_indicator["attr"]:
        ER_attr[item] = daynight_indicator["attr"][item]
    qcutils.CreateSeries(ds,"ER",ER,Flag=ER_flag,Attr=ER_attr)
    return 1

def check_for_missing_data(series_list,label_list):
    for item,label in zip(series_list,label_list):
        index = numpy.where(numpy.ma.getmaskarray(item)==True)[0]
        if len(index)!=0:
            log.error(" GetERFromFc: missing data in series "+label)
            return 0
    return 1

def get_ustar_thresholds(cf,ldt):
    if "cpd_filename" in cf["Files"]:
        ustar_dict = get_ustarthreshold_from_cpdresults(cf)
    else:
        msg = " CPD results filename not in control file"
        log.warning(msg)
        ustar_dict = get_ustarthreshold_from_cf(cf,ldt)
    cleanup_ustar_dict(ldt,ustar_dict)
    return ustar_dict

def get_ustar_thresholds2(cf,ldt):
    ustar_dict = get_ustarthreshold_from_cpdresults(cf)

    msg = " CPD results filename not in control file"
    log.warning(msg)
    ustar_dict = get_ustarthreshold_from_cf(cf,ldt)

    cleanup_ustar_dict(ldt,ustar_dict)

    return ustar_dict

def get_daynight_indicator(cf,Fsd,Fsd_syn,sa):
    # get the day/night indicator
    nRecs = len(Fsd)
    daynight_indicator = {"values":numpy.zeros(len(Fsd),dtype=numpy.int32),"attr":{}}
    inds = daynight_indicator["values"]
    attr = daynight_indicator["attr"]
    # get the filter type
    filter_type = qcutils.get_keyvaluefromcf(cf,["Options"],"DayNightFilter",default="Fsd")
    attr["daynight_filter"] = filter_type
    use_fsdsyn = qcutils.get_keyvaluefromcf(cf,["Options"],"UseFsdsyn_threshold",default="Yes")
    attr["use_fsdsyn"] = use_fsdsyn
    # get the indicator series
    if filter_type.lower()=="fsd":
        # get the Fsd threshold
        Fsd_threshold = int(qcutils.get_keyvaluefromcf(cf,["Options"],"Fsd_threshold",default=10))
        attr["Fsd_threshold"] = str(Fsd_threshold)
        # we are using Fsd only to define day/night
        if use_fsdsyn.lower()=="yes":
            idx = numpy.ma.where((Fsd<=Fsd_threshold)&(Fsd_syn<=Fsd_threshold))[0]
        else:
            idx = numpy.ma.where(Fsd<=Fsd_threshold)[0]
        inds[idx] = numpy.int32(1)
    elif filter_type.lower()=="sa":
        # get the solar altitude threshold
        sa_threshold = int(qcutils.get_keyvaluefromcf(cf,["Options"],"sa_threshold",default="-5"))
        attr["sa_threshold"] = str(sa_threshold)
        # we are using solar altitude to define day/night
        idx = numpy.ma.where(sa<sa_threshold)[0]
        inds[idx] = numpy.int32(1)
    else:
        msg = "Unrecognised DayNightFilter option in L6 control file"
        raise Exception(msg)
    return daynight_indicator

def get_day_indicator(cf,Fsd,Fsd_syn,sa):
    """
    Purpose:
     Returns a dictionary containing an indicator series and some attributes.
     The indicator series is 1 during day time and 0 at night time.  The threshold
     between night and day is the Fsd threshold specified in the control file.
    Usage:
     indicators["day"] = get_day_indicator(cf,Fsd,Fsd_syn,sa)
     where;
      cf is a control file object
      Fsd is a series of incoming shortwave radiation values (ndarray)
      Fsd_syn is a series of calculated Fsd (ndarray)
      sa is a series of solar altitude values (ndarray)
    and;
      indicators["day"] is a dictionary containing
      indicators["day"]["values"] is the indicator series
      indicators["day"]["attr"] are the attributes
    Author: PRI
    Date: March 2016
    """
    # indicator = 1 ==> day, indicator = 0 ==> night
    day_indicator = {"values":numpy.ones(len(Fsd),dtype=numpy.int32),
                     "attr":{}}
    inds = day_indicator["values"]
    attr = day_indicator["attr"]
    # get the filter type
    filter_type = qcutils.get_keyvaluefromcf(cf,["Options"],"DayNightFilter",default="Fsd")
    attr["daynight_filter_type"] = filter_type
    use_fsdsyn = qcutils.get_keyvaluefromcf(cf,["Options"],"UseFsdsyn_threshold",default="Yes")
    attr["use_fsdsyn"] = use_fsdsyn
    # get the indicator series
    if filter_type.lower()=="fsd":
        # get the Fsd threshold
        Fsd_threshold = int(qcutils.get_keyvaluefromcf(cf,["Options"],"Fsd_threshold",default=10))
        attr["Fsd_threshold"] = str(Fsd_threshold)
        # we are using Fsd only to define day/night
        if use_fsdsyn.lower()=="yes":
            idx = numpy.ma.where((Fsd<=Fsd_threshold)&(Fsd_syn<=Fsd_threshold))[0]
        else:
            idx = numpy.ma.where(Fsd<=Fsd_threshold)[0]
        inds[idx] = numpy.int32(0)
    elif filter_type.lower()=="sa":
        # get the solar altitude threshold
        sa_threshold = int(qcutils.get_keyvaluefromcf(cf,["Options"],"sa_threshold",default="-5"))
        attr["sa_threshold"] = str(sa_threshold)
        # we are using solar altitude to define day/night
        index = numpy.ma.where(sa<sa_threshold)[0]
        inds[index] = numpy.int32(0)
    else:
        msg = "Unrecognised DayNightFilter option in L6 control file"
        raise Exception(msg)
    return day_indicator

def get_evening_indicator(cf,Fsd,Fsd_syn,sa,ts):
    """
    Purpose:
     Returns a dictionary containing an indicator series and some attributes.
     The indicator series is 1 during the evening and 0 at all other times.
     Evening is defined as the period between sunset and the number of hours
     specified in the control file [Options] section as the EveningFilterLength
     key.
    Usage:
     indicators["evening"] = get_evening_indicator(cf,Fsd,Fsd_syn,sa,ts)
     where;
      cf is a control file object
      Fsd is a series of incoming shortwave radiation values (ndarray)
      Fsd_syn is a series of calculated Fsd (ndarray)
      sa is a series of solar altitude values (ndarray)
      ts is the time step (minutes), integer
    and;
      indicators["evening"] is a dictionary containing
      indicators["evening"]["values"] is the indicator series
      indicators["evening"]["attr"] are the attributes
    Author: PRI
    Date: March 2016
    """
    evening_indicator = {"values":numpy.zeros(len(Fsd),dtype=numpy.int32),
                         "attr":{}}
    attr = evening_indicator["attr"]
    opt = qcutils.get_keyvaluefromcf(cf,["Options"],"EveningFilterLength",default="0")
    num_hours = int(opt)
    if num_hours<=0 or num_hours>=12:
        evening_indicator = numpy.zeros(len(Fsd))
        msg = " Evening filter period outside 0 to 12 hours, skipping ..."
        log.warning(msg)
        return evening_indicator
    night_indicator = get_night_indicator(cf, Fsd, Fsd_syn, sa)
    day_indicator = get_day_indicator(cf, Fsd, Fsd_syn, sa)
    ntsperhour = int(0.5+float(60)/float(ts))
    shift = num_hours*ntsperhour
    day_indicator_shifted = numpy.roll(day_indicator["values"], shift)
    evening_indicator["values"] = night_indicator["values"]*day_indicator_shifted
    attr["evening_filter_length"] = num_hours
    return evening_indicator

def get_night_indicator(cf,Fsd,Fsd_syn,sa):
    """
    Purpose:
     Returns a dictionary containing an indicator series and some attributes.
     The indicator series is 1 during night time and 0 during the day.  The
     threshold for determining night and day is the Fsd threshold
     given in the control file [Options] section.
    Usage:
     indicators["night"] = get_night_indicator(cf,Fsd,Fsd_syn,sa)
     where;
      cf is a control file object
      Fsd is a series of incoming shortwave radiation values (ndarray)
      Fsd_syn is a series of calculated Fsd (ndarray)
      sa is a series of solar altitude values (ndarray)
    and;
      indicators["night"] is a dictionary containing
      indicators["night"]["values"] is the indicator series
      indicators["night"]["attr"] are the attributes
    Author: PRI
    Date: March 2016
    """
    # indicator = 1 ==> night, indicator = 0 ==> day
    night_indicator = {"values":numpy.zeros(len(Fsd),dtype=numpy.int32),
                       "attr":{}}
    inds = night_indicator["values"]
    attr = night_indicator["attr"]
    # get the filter type
    filter_type = qcutils.get_keyvaluefromcf(cf,["Options"],"DayNightFilter",default="Fsd")
    attr["daynight_filter_type"] = filter_type
    use_fsdsyn = qcutils.get_keyvaluefromcf(cf,["Options"],"UseFsdsyn_threshold",default="Yes")
    attr["use_fsdsyn"] = use_fsdsyn
    # get the indicator series
    if filter_type.lower()=="fsd":
        # get the Fsd threshold
        Fsd_threshold = int(qcutils.get_keyvaluefromcf(cf,["Options"],"Fsd_threshold",default=10))
        attr["Fsd_threshold"] = str(Fsd_threshold)
        # we are using Fsd only to define day/night
        if use_fsdsyn.lower()=="yes":
            idx = numpy.ma.where((Fsd<=Fsd_threshold)&(Fsd_syn<=Fsd_threshold))[0]
        else:
            idx = numpy.ma.where(Fsd<=Fsd_threshold)[0]
        inds[idx] = numpy.int32(1)
    elif filter_type.lower()=="sa":
        # get the solar altitude threshold
        sa_threshold = int(qcutils.get_keyvaluefromcf(cf,["Options"],"sa_threshold",default="-5"))
        attr["sa_threshold"] = str(sa_threshold)
        # we are using solar altitude to define day/night
        index = numpy.ma.where(sa<sa_threshold)[0]
        inds[index] = numpy.int32(1)
    else:
        msg = "Unrecognised DayNightFilter option in L6 control file"
        raise Exception(msg)
    return night_indicator

def get_turbulence_indicator(cf,ldt,ustar,L,ustar_dict,ts,attr):
    opt = qcutils.get_keyvaluefromcf(cf,["Options"],"TurbulenceFilter",default="ustar")
    if opt.lower()=="ustar":
        turbulence_indicator = get_turbulence_indicator_ustar(ldt,ustar,ustar_dict,ts,attr)
    elif opt.lower()=="l":
        msg = " GetERfromFc: use of L as turbulence indicator not implemented yet"
        log.warning(msg)
        #turbulence_indicator = get_turbulence_indicator_l(ldt,L,z,d,zmdonL_threshold)
    else:
        msg = " Unrecognised TurbulenceFilter option in control file"
        raise Exception(msg)
    return turbulence_indicator

def get_turbulence_indicator_l(ldt,L,z,d,zmdonL_threshold):
    turbulence_indicator = numpy.zeros(len(ldt),dtype=numpy.int32)
    zmdonL = (z-d)/L
    idx = numpy.ma.where(zmdonL<=zmdonL_threshold)[0]
    turbulence_indicator[idx] = numpy.int32(1)
    return turbulence_indicator

def get_turbulence_indicator_ustar(ldt,ustar,ustar_dict,ts):
    """
    Purpose:
     Returns a dictionary containing an indicator series and some attributes.
     The indicator series is 1 when ustar is above the threshold and 0 when
     ustar is below the threshold.
     By default, all day time observations are accepted regardless of ustar value.
    Usage:
     indicators["turbulence"] = get_turbulence_indicator_ustar(ldt,ustar,ustar_dict,ts)
     where;
      ldt is a list of datetimes
      ustar is a series of ustar values (ndarray)
      ustar_dict is a dictionary of ustar thresholds returned by qcrp.get_ustar_thresholds
      ts is the time step for ustar
    and;
     indicators["turbulence"] is a dictionary containing
      indicators["turbulence"]["values"] is the indicator series
      indicators["turbulence"]["attr"] are the attributes
    Author: PRI
    Date: March 2016
    """
    year_list = ustar_dict.keys()
    year_list.sort()
    # now loop over the years in the data to apply the ustar threshold
    turbulence_indicator = {"values":numpy.zeros(len(ldt)),"attr":{}}
    inds = turbulence_indicator["values"]
    attr = turbulence_indicator["attr"]
    attr["turbulence_filter"] = "ustar"
    for year in year_list:
        start_date = str(year)+"-01-01 00:30"
        if ts==60: start_date = str(year)+"-01-01 01:00"
        end_date = str(int(year)+1)+"-01-01 00:00"
        # get the ustar threshold
        ustar_threshold = float(ustar_dict[year]["ustar_mean"])
        attr["ustar_threshold_"+str(year)] = str(ustar_threshold)
        # get the start and end datetime indices
        si = qcutils.GetDateIndex(ldt,start_date,ts=ts,default=0,match='exact')
        ei = qcutils.GetDateIndex(ldt,end_date,ts=ts,default=len(ldt),match='exact')
        # set the QC flag
        idx = numpy.ma.where(ustar[si:ei]>=ustar_threshold)[0]
        inds[si:ei][idx] = numpy.int32(1)
    return turbulence_indicator

def get_turbulence_indicator_ustar_evg(ldt,ind_day,ind_ustar,ustar,ustar_dict,ts):
    """
    Purpose:
     Returns a dictionary containing an indicator series and some attributes.
     The indicator series is 1 when ustar is above the threshold after sunset
     and remains 1 until ustar falls below the threshold after which it remains
     0 until the following evening.
     By default, all day time observations are accepted regardless of ustar value.
     Based on a ustar filter scheme designed by Eva van Gorsel for use at the
     Tumbarumba site.
    Usage:
     indicators["turbulence"] = get_turbulence_indicator_ustar_evg(ldt,ind_day,ustar,ustar_dict,ts)
     where;
      ldt is a list of datetimes
      ind_day is a day/night indicator
      ustar is a series of ustar values (ndarray)
      ustar_dict is a dictionary of ustar thresholds returned by qcrp.get_ustar_thresholds
      ts is the time step for ustar
    and;
     indicators["turbulence"] is a dictionary containing
      indicators["turbulence"]["values"] is the indicator series
      indicators["turbulence"]["attr"] are the attributes
    Author: PRI, EVG, WW
    Date: December 2016
    """
    # differentiate the day/night indicator series, we will
    # use this value to indicate the transition from day to night
    dinds = numpy.ediff1d(ind_day, to_begin=0)
    # get the list of years
    year_list = ustar_dict.keys()
    year_list.sort()
    # now loop over the years in the data to apply the ustar threshold
    # ustar >= threshold ==> ind_ustar = 1 else ind_ustar = 0
    turbulence_indicator = {"values":ind_ustar,"attr":{}}
    attr = turbulence_indicator["attr"]
    attr["turbulence_filter"] = "ustar_evg"
    # get an array of ustar threshold values
    year = numpy.array([ldt[i].year for i in range(len(ldt))])
    ustar_threshold = numpy.zeros(len(ldt))
    for yr in year_list:
        idx = numpy.where(year==int(yr))[0]
        ustar_threshold[idx] = float(ustar_dict[yr]["ustar_mean"])
        attr["ustar_threshold_"+str(yr)] = str(ustar_dict[yr]["ustar_mean"])
    # get the indicator series
    ind_evg = ind_day.copy()
    idx = numpy.where(dinds<-0.5)[0]
    for i in idx:
        n = i
        while ustar[n]>=ustar_threshold[n]:
            ind_evg[n] = 1
            n = n+1
            if n>=len(ldt):
                break
    turbulence_indicator["values"] = turbulence_indicator["values"]*ind_evg
    return turbulence_indicator

def get_ustarthreshold_from_cf(cf,ldt):
    """
    Purpose:
     Returns a dictionary containing ustar thresholds for each year read from
     the control file.  If no [ustar_threshold] section is found then a
     default value of 0.25 is used.
    Usage:
     ustar_dict = qcrp.get_ustarthreshold_from_cf(cf,ldt)
     where cf is the control file object
           ldt is the Python datetime series from the data structure
    Author: PRI
    Date: July 2015
    """
    ustar_dict = collections.OrderedDict()
    ustar_threshold_list = []
    if "ustar_threshold" in cf.keys():
        msg = " Using values from ustar_threshold section"
        log.info(msg)
        for n in cf["ustar_threshold"].keys():
            ustar_threshold_list.append(ast.literal_eval(cf["ustar_threshold"][str(n)]))
        for item in ustar_threshold_list:
            startdate = dateutil.parser.parse(item[0])
            year = startdate.year
            ustar_dict[str(year)] = {}
            ustar_dict[str(year)]["ustar_mean"] = float(item[2])
    else:
        log.error(" No [ustar_threshold] section in control file")
        log.error(" ... using default value of 0.25 m/s")
        startyear = ldt[0].year
        endyear = ldt[-1].year
        years = range(startyear,endyear+1)
        for year in years:
            ustar_dict[str(year)] = {}
            ustar_dict[str(year)]["ustar_mean"] = float(0.25)
    return ustar_dict

def get_ustarthreshold_from_cpdresults(cf):
    """
    Purpose:
     Returns a dictionary containing ustar thresholds for each year read from
     the CPD results file.  If there is no CPD results file name found in the
     control file then return an empty dictionary
    Usage:
     ustar_dict = qcrp.get_ustarthreshold_from_cpdresults(cf)
     where cf is the control file object
           ustar_dict is a dictionary of ustar thtresholds, 1 entry per year
    Author: PRI
    Date: July 2015
    """
    ustar_dict = collections.OrderedDict()
    if "cpd_filename" not in cf["Files"]:
        msg = " CPD results filename not in control file"
        log.warning(msg)
        return ustar_dict
    cpd_path = cf["Files"]["file_path"]
    cpd_name = cpd_path+cf["Files"]["cpd_filename"]
    cpd_wb = xlrd.open_workbook(cpd_name)
    annual_ws = cpd_wb.sheet_by_name("Annual")
    header_list = [x for x in annual_ws.row_values(0)]
    year_list = [str(int(x)) for x in annual_ws.col_values(0)[1:]]
    for i,year in enumerate(year_list):
        ustar_dict[year] = collections.OrderedDict()
        for item in header_list:
            xlcol = header_list.index(item)
            val = annual_ws.col_values(xlcol)[i+1]
            typ = annual_ws.col_types(xlcol)[i+1]
            if typ==2:
                ustar_dict[year][item] = float(val)
            else:
                ustar_dict[year][item] = float(c.missing_value)
    return ustar_dict

def get_ustar_thresholds_annual(ldt,ustar_threshold):
    """
    Purpose:
     Returns a dictionary containing ustar thresholds for all years using
     a single value enetred as the ustar_threshold argument.
    Usage:
     ustar_dict = qcrp.get_ustar_thresholds_annual(ldt,ustar_threshold)
     where ldt is a list of datetime objects
           ustar_threshold is the value to be used
    Author: PRI
    Date: July 2015
    """
    ustar_dict = collections.OrderedDict()
    if not isinstance(ustar_threshold,float):
        ustar_threshold = float(ustar_threshold)
    start_year = ldt[0].year
    end_year = ldt[-1].year
    for year in range(start_year,end_year+1):
        ustar_dict[year] = {}
        ustar_dict[year]["ustar_mean"] = ustar_threshold
    return ustar_dict

def L6_summary(cf,ds):
    """
    Purpose:
     Produce summaries of L6 data, write them to an Excel spreadsheet and plot them.
    Usage:
    Author: PRI
    Date: June 2015
    """
    log.info(" Doing the L6 summary")
    # set up a dictionary of lists
    series_dict = L6_summary_createseriesdict(cf,ds)
    # open the Excel workbook
    nc_name = qcio.get_outfilenamefromcf(cf)
    xl_name = nc_name.replace(".nc","_Summary.xls")
    xl_file = qcio.xl_open_write(xl_name)
    if xl_file=='':
        log.error("L6_summary: error opening Excel file "+xl_name)
        return
    # daily averages and totals
    daily_dict = L6_summary_daily(ds,series_dict)
    L6_summary_write_xlfile(xl_file,"Daily (all)",daily_dict)
    #flag_dict = L6_summary_daily_flag(ds,series_dict)
    fluxes_dict = L6_summary_co2andh2o_fluxes(ds,series_dict,daily_dict)
    L6_summary_write_xlfile(xl_file,"Daily (CO2,H2O)",fluxes_dict)
    # monthly averages and totals
    monthly_dict = L6_summary_monthly(ds,series_dict)
    L6_summary_write_xlfile(xl_file,"Monthly",monthly_dict)
    # annual averages and totals
    annual_dict = L6_summary_annual(ds,series_dict)
    L6_summary_write_xlfile(xl_file,"Annual",annual_dict)
    # cumulative totals
    cumulative_dict = L6_summary_cumulative(ds,series_dict)
    for year in cumulative_dict.keys():
        L6_summary_write_xlfile(xl_file,"Cummulative("+str(year)+")",cumulative_dict[str(year)])
    # close the Excel workbook
    xl_file.save(xl_name)
    # plot the daily averages and sums
    L6_summary_plotdaily(cf,ds,daily_dict)
    # plot the cumulative sums
    L6_summary_plotcumulative(cf,ds,cumulative_dict)

def L6_summary_plotdaily(cf,ds,daily_dict):
    """
    Purpose:
     Plot the daily averages or sums with a 30 day filter.
    Usage:
     L6_summary_plotdaily(daily_dict)
     where daily_dict is the dictionary of results returned by L6_summary_daily
    Author: PRI
    Date: June 2015
    """
    type_list = []
    for item in daily_dict.keys():
        if item[0:2]=="ER": type_list.append(item[2:])
    for item in type_list:
        if "NEE"+item not in daily_dict or "GPP"+item not in daily_dict:
            type_list.remove(item)
    # plot time series of NEE, GPP and ER
    sdate = daily_dict["DateTime"]["data"][0].strftime("%d-%m-%Y")
    edate = daily_dict["DateTime"]["data"][-1].strftime("%d-%m-%Y")
    site_name = ds.globalattributes["site_name"]
    title_str = site_name+": "+sdate+" to "+edate
    for item in type_list:
        if cf["Options"]["call_mode"].lower()=="interactive":
            plt.ion()
        else:
            plt.ioff()
        fig = plt.figure(figsize=(16,4))
        fig.canvas.set_window_title("Carbon Budget: "+item.replace("_",""))
        plt.figtext(0.5,0.95,title_str,horizontalalignment='center')
        plt.plot(daily_dict["DateTime"]["data"],daily_dict["NEE"+item]["data"],'b-',alpha=0.3)
        plt.plot(daily_dict["DateTime"]["data"],qcts.smooth(daily_dict["NEE"+item]["data"],window_len=30),
                 'b-',linewidth=2,label="NEE"+item+" (30 day filter)")
        plt.plot(daily_dict["DateTime"]["data"],daily_dict["GPP"+item]["data"],'g-',alpha=0.3)
        plt.plot(daily_dict["DateTime"]["data"],qcts.smooth(daily_dict["GPP"+item]["data"],window_len=30),
                 'g-',linewidth=2,label="GPP"+item+" (30 day filter)")
        plt.plot(daily_dict["DateTime"]["data"],daily_dict["ER"+item]["data"],'r-',alpha=0.3)
        plt.plot(daily_dict["DateTime"]["data"],qcts.smooth(daily_dict["ER"+item]["data"],window_len=30),
                 'r-',linewidth=2,label="ER"+item+" (30 day filter)")
        plt.axhline(0)
        plt.xlabel("Date")
        plt.ylabel(daily_dict["NEE"+item]["units"])
        plt.legend(loc='upper left',prop={'size':8})
        plt.tight_layout()
        sdt = daily_dict["DateTime"]["data"][0].strftime("%Y%m%d")
        edt = daily_dict["DateTime"]["data"][-1].strftime("%Y%m%d")
        plot_path = cf["Files"]["plot_path"]+"L6/"
        if not os.path.exists(plot_path): os.makedirs(plot_path)
        figname = plot_path+site_name.replace(" ","")+"_CarbonBudget"+item
        figname = figname+"_"+sdt+"_"+edt+'.png'
        fig.savefig(figname,format='png')
        if cf["Options"]["call_mode"].lower=="interactive":
            plt.draw()
            plt.ioff()
        else:
            plt.ion()
    # plot time series of Fn,Fg,Fh,Fe
    if cf["Options"]["call_mode"].lower()=="interactive":
        plt.ion()
    else:
        plt.ioff()
    fig = plt.figure(figsize=(16,4))
    fig.canvas.set_window_title("Surface Energy Budget")
    plt.figtext(0.5,0.95,title_str,horizontalalignment='center')
    plt.plot(daily_dict["DateTime"]["data"],daily_dict["Fn"]["data"],'k-',alpha=0.3)
    plt.plot(daily_dict["DateTime"]["data"],qcts.smooth(daily_dict["Fn"]["data"],window_len=30),
             'k-',linewidth=2,label="Fn (30 day filter)")
    plt.plot(daily_dict["DateTime"]["data"],daily_dict["Fg"]["data"],'g-',alpha=0.3)
    plt.plot(daily_dict["DateTime"]["data"],qcts.smooth(daily_dict["Fg"]["data"],window_len=30),
             'g-',linewidth=2,label="Fg (30 day filter)")
    plt.plot(daily_dict["DateTime"]["data"],daily_dict["Fh"]["data"],'r-',alpha=0.3)
    plt.plot(daily_dict["DateTime"]["data"],qcts.smooth(daily_dict["Fh"]["data"],window_len=30),
             'r-',linewidth=2,label="Fh (30 day filter)")
    plt.plot(daily_dict["DateTime"]["data"],daily_dict["Fe"]["data"],'b-',alpha=0.3)
    plt.plot(daily_dict["DateTime"]["data"],qcts.smooth(daily_dict["Fe"]["data"],window_len=30),
             'b-',linewidth=2,label="Fe (30 day filter)")
    plt.xlabel("Date")
    plt.ylabel(daily_dict["Fn"]["units"])
    plt.legend(loc='upper left',prop={'size':8})
    plt.tight_layout()
    sdt = daily_dict["DateTime"]["data"][0].strftime("%Y%m%d")
    edt = daily_dict["DateTime"]["data"][-1].strftime("%Y%m%d")
    plot_path = cf["Files"]["plot_path"]+"L6/"
    if not os.path.exists(plot_path): os.makedirs(plot_path)
    figname = plot_path+site_name.replace(" ","")+"_SEB"
    figname = figname+"_"+sdt+"_"+edt+'.png'
    fig.savefig(figname,format='png')
    if cf["Options"]["call_mode"].lower=="interactive":
        plt.draw()
        plt.ioff()
    else:
        plt.ion()

def L6_summary_plotcumulative(cf,ds,cumulative_dict):
    ts = int(ds.globalattributes["time_step"])
    # cumulative plots
    color_list = ["blue","red","green","yellow","magenta","black","cyan","brown"]
    year_list = cumulative_dict.keys()
    year_list.sort()
    type_list = []
    for item in cumulative_dict[year_list[0]].keys():
        if item[0:2]=="ER": type_list.append(item[2:])
    for item in type_list:
        if "NEE"+item not in cumulative_dict[year_list[0]] or "GPP"+item not in cumulative_dict[year_list[0]]:
            type_list.remove(item)
    # do the plots
    site_name = ds.globalattributes["site_name"]
    title_str = site_name+": "+year_list[0]+" to "+year_list[-1]
    for item in type_list:
        if cf["Options"]["call_mode"].lower()=="interactive":
            plt.ion()
        else:
            plt.ioff()
        fig = plt.figure(figsize=(12,12))
        fig.canvas.set_window_title("Cumulative plots: "+item.replace("_",""))
        plt.suptitle(title_str)
        plt.subplot(221)
        plt.title("NEE: "+item.replace("_",""),fontsize=12)
        for n,year in enumerate(year_list):
            x = numpy.arange(0,len(cumulative_dict[year]["NEE"+item]["data"]))*ts/float(60)
            plt.plot(x,cumulative_dict[year]["NEE"+item]["data"],color=color_list[numpy.mod(n,8)],
                     label=str(year))
            plt.xlabel("Hour of Year")
            plt.ylabel(cumulative_dict[year]["NEE"+item]["units"])
            plt.legend(loc='lower left',prop={'size':8})
        plt.subplot(222)
        plt.title("GPP: "+item.replace("_",""),fontsize=12)
        for n,year in enumerate(year_list):
            x = numpy.arange(0,len(cumulative_dict[year]["GPP"+item]["data"]))*ts/float(60)
            plt.plot(x,cumulative_dict[year]["GPP"+item]["data"],color=color_list[numpy.mod(n,8)],
                     label=str(year))
            plt.xlabel("Hour of Year")
            plt.ylabel(cumulative_dict[year]["GPP"+item]["units"])
            plt.legend(loc='lower right',prop={'size':8})
        plt.subplot(223)
        plt.title("ER: "+item.replace("_",""),fontsize=12)
        for n,year in enumerate(year_list):
            x = numpy.arange(0,len(cumulative_dict[year]["ER"+item]["data"]))*ts/float(60)
            plt.plot(x,cumulative_dict[year]["ER"+item]["data"],color=color_list[numpy.mod(n,8)],
                     label=str(year))
            plt.xlabel("Hour of Year")
            plt.ylabel(cumulative_dict[year]["ER"+item]["units"])
            plt.legend(loc='lower right',prop={'size':8})
        plt.subplot(224)
        plt.title("ET & Precip",fontsize=12)
        for n,year in enumerate(year_list):
            x = numpy.arange(0,len(cumulative_dict[year]["ET"]["data"]))*ts/float(60)
            plt.plot(x,cumulative_dict[year]["ET"]["data"],color=color_list[numpy.mod(n,8)],
                     label=str(year))
            plt.plot(x,cumulative_dict[year]["Precip"]["data"],color=color_list[numpy.mod(n,8)],
                     linestyle='--')
            plt.xlabel("Hour of Year")
            plt.ylabel(cumulative_dict[year]["ET"]["units"])
            plt.legend(loc='upper left',prop={'size':8})
        plt.tight_layout(rect=[0, 0, 1, 0.98])
        # save a hard copy of the plot
        sdt = year_list[0]
        edt = year_list[-1]
        plot_path = cf["Files"]["plot_path"]+"L6/"
        if not os.path.exists(plot_path): os.makedirs(plot_path)
        figname = plot_path+site_name.replace(" ","")+"_Cumulative_"+item.replace("_","")
        figname = figname+"_"+sdt+"_"+edt+'.png'
        fig.savefig(figname,format='png')
        if cf["Options"]["call_mode"].lower=="interactive":
            plt.draw()
            plt.ioff()
        else:
            plt.ion()

def L6_summary_createseriesdict(cf,ds):
    """
    Purpose:
     Create a dictionary containing lists of variables, operators and formats
    for use by the daily, annual and cumulative routines.
    Usage:
     series_dict = L6_summary_createseriesdict(cf,ds)
     where cf is a control file object
           ds is an OzFluxQC data structure
           series_dict is a dictionary of various variable lists
    Author: PRI
    Date: June 2015
    """
    ts = int(ds.globalattributes["time_step"])
    series_dict = {"daily":{},"annual":{},"cumulative":{},"lists":{}}
    # adjust units of NEE, NEP, GPP and ER
    sdl = series_dict["lists"]
    sdl["nee"] = [item for item in cf["NEE"].keys() if "NEE" in item and item in ds.series.keys()]
    sdl["gpp"] = [item for item in cf["GPP"].keys() if "GPP" in item and item in ds.series.keys()]
    sdl["fre"] = [item for item in cf["ER"].keys() if "ER" in item and item in ds.series.keys()]
    sdl["nep"] = [item.replace("NEE","NEP") for item in sdl["nee"]]
    sdl["nep"] = [item for item in sdl["nep"] if item in ds.series.keys()]
    sdl["co2"] = sdl["nee"]+sdl["nep"]+sdl["gpp"]+sdl["fre"]
    for item in sdl["co2"]:
        series_dict["daily"][item] = {}
        series_dict["cumulative"][item] = {}
        series_dict["daily"][item]["operator"] = "sum"
        series_dict["daily"][item]["format"] = "0.00"
        series_dict["cumulative"][item]["operator"] = "sum"
        series_dict["cumulative"][item]["format"] = "0.00"
    sdl["ET"] = [item for item in ds.series.keys() if "ET" in item]
    sdl["Precip"] = [item for item in ds.series.keys() if "Precip" in item]
    sdl["h2o"] = sdl["ET"]+sdl["Precip"]
    for item in sdl["h2o"]:
        series_dict["daily"][item] = {"operator":"sum","format":"0.00"}
        series_dict["cumulative"][item] = {"operator":"sum","format":"0.00"}
    if "Ah" in ds.series.keys():
        series_dict["daily"]["Ah"] = {"operator":"average","format":"0.00"}
    if "Cc" in ds.series.keys():
        series_dict["daily"]["Cc"] = {"operator":"average","format":"0.0"}
    if "Fc" in ds.series.keys():
        series_dict["daily"]["Fc"] = {"operator":"average","format":"0.00"}
    if "Fe" in ds.series.keys():
        series_dict["daily"]["Fe"] = {"operator":"average","format":"0.0"}
    if "Fh" in ds.series.keys():
        series_dict["daily"]["Fh"] = {"operator":"average","format":"0.0"}
    if "Fg" in ds.series.keys():
        series_dict["daily"]["Fg"] = {"operator":"average","format":"0.0"}
    if "Fn" in ds.series.keys():
        series_dict["daily"]["Fn"] = {"operator":"average","format":"0.0"}
    if "Fsd" in ds.series.keys():
        series_dict["daily"]["Fsd"] = {"operator":"average","format":"0.0"}
    if "Fsu" in ds.series.keys():
        series_dict["daily"]["Fsu"] = {"operator":"average","format":"0.0"}
    if "Fld" in ds.series.keys():
        series_dict["daily"]["Fld"] = {"operator":"average","format":"0.0"}
    if "Flu" in ds.series.keys():
        series_dict["daily"]["Flu"] = {"operator":"average","format":"0.0"}
    if "ps" in ds.series.keys():
        series_dict["daily"]["ps"] = {"operator":"average","format":"0.00"}
    if "q" in ds.series.keys():
        series_dict["daily"]["q"] = {"operator":"average","format":"0.0000"}
    if "RH" in ds.series.keys():
        series_dict["daily"]["RH"] = {"operator":"average","format":"0"}
    if "Sws" in ds.series.keys():
        series_dict["daily"]["Sws"] = {"operator":"average","format":"0.000"}
    if "Ta" in ds.series.keys():
        series_dict["daily"]["Ta"] = {"operator":"average","format":"0.00"}
    if "Ts" in ds.series.keys():
        series_dict["daily"]["Ts"] = {"operator":"average","format":"0.00"}
    if "ustar" in ds.series.keys():
        series_dict["daily"]["ustar"] = {"operator":"average","format":"0.00"}
    if "Ws" in ds.series.keys():
        series_dict["daily"]["Ws"] = {"operator":"average","format":"0.00"}
    series_dict["annual"] = series_dict["daily"]
    series_dict["monthly"] = series_dict["daily"]
    return series_dict

def L6_summary_daily(ds,series_dict):
    """
    Purpose:
     Calculate the daily averages or sums of various quantities and write
     them to a worksheet in an Excel workbook.
    Usage:
     L6_summary_daily(ds,series_dict)
     where ds is an OzFluxQC data structure
           series_dict is a dictionary of various variable lists
    Author: PRI
    Date: June 2015
    """
    log.info(" Doing the daily summary (data) at L6")
    dt = ds.series["DateTime"]["Data"]
    ts = int(ds.globalattributes["time_step"])
    si = qcutils.GetDateIndex(dt,str(dt[0]),ts=ts,default=0,match="startnextday")
    ei = qcutils.GetDateIndex(dt,str(dt[-1]),ts=ts,default=len(dt)-1,match="endpreviousday")
    ldt = dt[si:ei+1]
    ntsInDay = int(24.0*60.0/float(ts))
    nDays = int(len(ldt))/ntsInDay
    ldt_daily = [ldt[0]+datetime.timedelta(days=i) for i in range(0,nDays)]
    daily_dict = {}
    daily_dict["DateTime"] = {"data":ldt_daily,"units":"Days","format":"dd/mm/yyyy"}
    series_list = series_dict["daily"].keys()
    series_list.sort()
    for item in series_list:
        if item not in ds.series.keys(): continue
        daily_dict[item] = {}
        data_1d,flag_1d,attr = qcutils.GetSeriesasMA(ds,item,si=si,ei=ei)
        if item in series_dict["lists"]["co2"]:
            data_1d = qcutils.convert_units_func(ds,data_1d,attr["units"],"gC/m2",ts)
            daily_dict[item]["units"] = "gC/m2"
        else:
            daily_dict[item]["units"] = attr["units"]
        data_2d = data_1d.reshape(nDays,ntsInDay)
        if series_dict["daily"][item]["operator"].lower()=="average":
            daily_dict[item]["data"] = numpy.ma.average(data_2d,axis=1)
        elif series_dict["daily"][item]["operator"].lower()=="sum":
            daily_dict[item]["data"] = numpy.ma.sum(data_2d,axis=1)
            daily_dict[item]["units"] = daily_dict[item]["units"]+"/day"
        else:
            msg = "Unrecognised operator ("+series_dict["daily"][item]["operator"]
            msg = msg+") for series "+item
            log.error(msg)
            continue
        # add the format to be used
        daily_dict[item]["format"] = series_dict["daily"][item]["format"]
        # now do the flag, this is the fraction of data with QC flag = 0 in the day
        daily_dict[item]["flag"] = numpy.zeros(nDays,dtype=numpy.float64)
        flag_2d = flag_1d.reshape(nDays,ntsInDay)
        for i in range(nDays):
            daily_dict[item]["flag"][i] = 1-float(numpy.count_nonzero(flag_2d[i,:]))/float(ntsInDay)
    return daily_dict

def L6_summary_co2andh2o_fluxes(ds,series_dict,daily_dict):
    """
    Purpose:
    Usage:
    Author: PRI
    Date: March 2016
    """
    log.info(" Doing the daily summary (fluxes) at L6")
    sdl = series_dict["lists"]
    series_list = sdl["h2o"]+sdl["co2"]
    fluxes_dict = {}
    fluxes_dict["DateTime"] = daily_dict["DateTime"]
    for item in series_list:
        fluxes_dict[item] = {}
        fluxes_dict[item]["data"] = daily_dict[item]["data"]
        fluxes_dict[item]["units"] = daily_dict[item]["units"]
        fluxes_dict[item]["format"] = daily_dict[item]["format"]
        fluxes_dict[item+"_flag"] = {}
        fluxes_dict[item+"_flag"]["data"] = daily_dict[item]["flag"]
        fluxes_dict[item+"_flag"]["units"] = "frac"
        fluxes_dict[item+"_flag"]["format"] = "0.00"
    return fluxes_dict

def L6_summary_write_xlfile(xl_file,sheet_name,data_dict):
    # add the daily worksheet to the summary Excel file
    xl_sheet = xl_file.add_sheet(sheet_name)
    qcio.xl_write_data(xl_sheet,data_dict)

def L6_summary_monthly(ds,series_dict):
    """
    Purpose:
     Calculate the monthly averages or sums of various quantities and write
     them to a worksheet in an Excel workbook.
    Usage:
     L6_summary_monthly(ds,series_dict)
     where ds is an OzFluxQC data structure
           series_dict is a dictionary of various variable lists
    Author: PRI
    Date: July 2015
    """
    log.info(" Doing the monthly summaries at L6")
    dt = ds.series["DateTime"]["Data"]
    ts = int(ds.globalattributes["time_step"])
    si = qcutils.GetDateIndex(dt,str(dt[0]),ts=ts,default=0,match="startnextmonth")
    ldt = dt[si:]
    monthly_dict = {}
    monthly_dict["DateTime"] = {"data":[],"units":"Months","format":"dd/mm/yyyy"}
    # create arrays in monthly_dict
    series_list = series_dict["monthly"].keys()
    series_list.sort()
    # create the data arrays
    for item in series_list:
        monthly_dict[item] = {"data":numpy.ma.array([])}
    # loop over the months in the data file
    start_date = ldt[0]
    end_date = start_date+dateutil.relativedelta.relativedelta(months=1)
    end_date = end_date-dateutil.relativedelta.relativedelta(minutes=ts)
    last_date = ldt[-1]
    while start_date<=last_date:
        # *** The Elise Pendall bug fix ***
        si = qcutils.GetDateIndex(dt,str(start_date),ts=ts,default=0)
        ei = qcutils.GetDateIndex(dt,str(end_date),ts=ts,default=len(dt)-1)
        monthly_dict["DateTime"]["data"].append(dt[si])
        for item in series_list:
            if item not in ds.series.keys(): continue
            data_1d,flag,attr = qcutils.GetSeriesasMA(ds,item,si=si,ei=ei)
            if item in series_dict["lists"]["co2"]:
                data_1d = qcutils.convert_units_func(ds,data_1d,attr["units"],"gC/m2",ts)
                monthly_dict[item]["units"] = "gC/m2"
            else:
                monthly_dict[item]["units"] = attr["units"]
            if series_dict["monthly"][item]["operator"].lower()=="average":
                monthly_dict[item]["data"] = numpy.append(monthly_dict[item]["data"],
                                                          numpy.ma.average(data_1d))
            elif series_dict["monthly"][item]["operator"].lower()=="sum":
                monthly_dict[item]["data"] = numpy.append(monthly_dict[item]["data"],
                                                          numpy.ma.sum(data_1d))
                monthly_dict[item]["units"] = monthly_dict[item]["units"]+"/month"
            else:
                print "unrecognised operator"
            monthly_dict[item]["format"] = series_dict["monthly"][item]["format"]
        start_date = end_date+dateutil.relativedelta.relativedelta(minutes=ts)
        end_date = start_date+dateutil.relativedelta.relativedelta(months=1)
        end_date = end_date-dateutil.relativedelta.relativedelta(minutes=ts)
    return monthly_dict

def L6_summary_annual(ds,series_dict):
    """
    Purpose:
     Calculate the annual averages or sums of various quantities and write
     them to a worksheet in an Excel workbook.
    Usage:
     L6_summary_annual(ds,series_dict)
     where ds is an OzFluxQC data structure
           series_dict is a dictionary of various variable lists
    Author: PRI
    Date: June 2015
    """
    log.info(" Doing the annual summaries at L6")
    dt = ds.series["DateTime"]["Data"]
    ts = int(ds.globalattributes["time_step"])
    nperDay = int(24/(float(ts)/60.0)+0.5)
    si = qcutils.GetDateIndex(dt,str(dt[0]),ts=ts,default=0,match="startnextday")
    ei = qcutils.GetDateIndex(dt,str(dt[-1]),ts=ts,default=len(dt)-1,match="endpreviousday")
    ldt = dt[si:ei+1]
    start_year = ldt[0].year
    end_year = ldt[-1].year
    year_list = range(start_year,end_year+1,1)
    annual_dict = {}
    annual_dict["DateTime"] = {"data":[datetime.datetime(yr,1,1) for yr in year_list],
                               "units":"Years","format":"dd/mm/yyyy"}
    annual_dict["nDays"] = {"data":numpy.array([float(-9999)]*len(year_list)),
                            "units":"","format":"0"}
    # create arrays in annual_dict
    series_list = series_dict["annual"].keys()
    series_list.sort()
    for item in series_list:
        annual_dict[item] = {"data":numpy.ma.array([float(-9999)]*len(year_list))}
    for i,year in enumerate(year_list):
        if ts==30:
            start_date = str(year)+"-01-01 00:30"
        elif ts==60:
            start_date = str(year)+"-01-01 01:00"
        end_date = str(year+1)+"-01-01 00:00"
        si = qcutils.GetDateIndex(dt,start_date,ts=ts,default=0)
        ei = qcutils.GetDateIndex(dt,end_date,ts=ts,default=len(dt)-1)
        nDays = int((ei-si+1)/nperDay+0.5)
        annual_dict["nDays"]["data"][i] = nDays
        for item in series_list:
            if item not in ds.series.keys(): continue
            data_1d,flag,attr = qcutils.GetSeriesasMA(ds,item,si=si,ei=ei)
            if item in series_dict["lists"]["co2"]:
                data_1d = qcutils.convert_units_func(ds,data_1d,attr["units"],"gC/m2",ts)
                annual_dict[item]["units"] = "gC/m2"
            else:
                annual_dict[item]["units"] = attr["units"]
            if series_dict["annual"][item]["operator"].lower()=="average":
                annual_dict[item]["data"][i] = numpy.ma.average(data_1d)
            elif series_dict["annual"][item]["operator"].lower()=="sum":
                annual_dict[item]["data"][i] = numpy.ma.sum(data_1d)
                annual_dict[item]["units"] = annual_dict[item]["units"]+"/year"
            else:
                print "unrecognised operator"
            annual_dict[item]["format"] = series_dict["annual"][item]["format"]
    return annual_dict

def L6_summary_cumulative(ds,series_dict):
    """
    Purpose:
     Calculate the cumulative sums of various quantities and write
     them to a worksheet in an Excel workbook.
    Usage:
     L6_summary_cumulative(xl_file,ds,series_dict)
     where xl_file is an Excel file object
           ds is an OzFluxQC data structure
           series_dict is a dictionary of various variable lists
    Author: PRI
    Date: June 2015
    """
    log.info(" Doing the cumulative summaries at L6")
    dt = ds.series["DateTime"]["Data"]
    ts = int(ds.globalattributes["time_step"])
    si = qcutils.GetDateIndex(dt,str(dt[0]),ts=ts,default=0,match="startnextday")
    ei = qcutils.GetDateIndex(dt,str(dt[-1]),ts=ts,default=len(dt)-1,match="endpreviousday")
    ldt = dt[si:ei+1]
    start_year = ldt[0].year
    end_year = ldt[-1].year
    year_list = range(start_year,end_year+1,1)
    series_list = series_dict["cumulative"].keys()
    cumulative_dict = {}
    for i,year in enumerate(year_list):
        cumulative_dict[str(year)] = {}
        if ts==30:
            start_date = str(year)+"-01-01 00:30"
        elif ts==60:
            start_date = str(year)+"-01-01 01:00"
        end_date = str(year+1)+"-01-01 00:00"
        si = qcutils.GetDateIndex(dt,start_date,ts=ts,default=0)
        ei = qcutils.GetDateIndex(dt,end_date,ts=ts,default=len(dt)-1)
        ldt = dt[si:ei+1]
        cumulative_dict[str(year)]["DateTime"] = {"data":ldt,"units":"Year",
                                                  "format":"dd/mm/yyyy HH:MM"}
        for item in series_list:
            cumulative_dict[str(year)][item] = {}
            data,flag,attr = qcutils.GetSeriesasMA(ds,item,si=si,ei=ei)
            if item in series_dict["lists"]["co2"]:
                data = qcutils.convert_units_func(ds,data,attr["units"],"gC/m2",ts)
                cumulative_dict[str(year)][item]["units"] = "gC/m2"
            else:
                cumulative_dict[str(year)][item]["units"] = attr["units"]
            cumulative_dict[str(year)][item]["data"] = numpy.ma.cumsum(data)
            cumulative_dict[str(year)][item]["format"] = series_dict["cumulative"][item]["format"]
            cumulative_dict[str(year)][item]["units"] = cumulative_dict[str(year)][item]["units"]+"/year"
    return cumulative_dict

def ParseL6ControlFile(cf,ds):
    """ Parse the L6 control file. """
    # start with the repiration section
    if "Respiration" in cf.keys() and "ER" not in cf.keys(): cf["ER"] = cf["Respiration"]
    if "ER" in cf.keys():
        for ThisOne in cf["ER"].keys():
            if "ERUsingSOLO" in cf["ER"][ThisOne].keys():
                qcrpNN.rpSOLO_createdict(cf,ds,ThisOne)      # create the SOLO dictionary in ds
            if "ERUsingFFNET" in cf["ER"][ThisOne].keys():
                qcrpNN.rpFFNET_createdict(cf,ds,ThisOne)     # create the FFNET dictionary in ds
            if "ERUsingLloydTaylor" in cf["ER"][ThisOne].keys():
                qcrpLT.rpLT_createdict(cf,ds,ThisOne)        # create the Lloyd-Taylor dictionary in ds
            if "ERUsingLasslop" in cf["ER"][ThisOne].keys():
                qcrpLL.rpLL_createdict(cf,ds,ThisOne)        # create the Lasslop dictionary in ds
    if "NEE" in cf.keys():
        for ThisOne in cf["NEE"].keys():
            rpNEE_createdict(cf,ds,ThisOne)
    if "GPP" in cf.keys():
        for ThisOne in cf["GPP"].keys():
            rpGPP_createdict(cf,ds,ThisOne)

def PartitionNEE(cf,ds):
    """
    Purpose:
     Partition NEE into GPP and ER.
     Input and output names are held in ds.nee.
    Usage:
     qcrp.PartitionNEE(cf,ds)
      where cf is a conbtrol file object
            ds is a data structure
    Side effects:
     Series to hold the GPP data are created in ds.
    Author: PRI
    Date: August 2014
    """
    if "gpp" not in dir(ds): return
    # get the Fsd threshold
    opt = qcutils.get_keyvaluefromcf(cf,["Options"],"Fsd_threshold",default=10)
    Fsd_threshold = float(opt)
    # get the incoming shortwave radiation
    Fsd,Fsd_flag,Fsd_attr = qcutils.GetSeriesasMA(ds,"Fsd")
    if "Fsd_syn" in ds.series.keys():
        Fsd_syn,flag,attr = qcutils.GetSeriesasMA(ds,"Fsd_syn")
        index = numpy.where(numpy.ma.getmaskarray(Fsd)==True)[0]
        #index = numpy.ma.where(numpy.ma.getmaskarray(Fsd)==True)[0]
        Fsd[index] = Fsd_syn[index]
    # calculate GPP from NEE and ER
    for label in ds.gpp.keys():
        if "NEE" not in ds.gpp[label] and "ER" not in ds.gpp[label]: continue
        NEE_label = ds.gpp[label]["NEE"]
        ER_label = ds.gpp[label]["ER"]
        output_label = ds.gpp[label]["output"]
        NEE,NEE_flag,NEE_attr = qcutils.GetSeriesasMA(ds,NEE_label)
        ER,ER_flag,ER_attr = qcutils.GetSeriesasMA(ds,ER_label)
        # calculate GPP
        # here we use the conventions from Chapin et al (2006)
        #  NEP = -1*NEE
        #  GPP = NEP + ER ==> GPP = -1*NEE + ER
        GPP = float(-1)*NEE + ER
        ds.series[output_label]["Data"] = GPP
        ds.series[output_label]["Flag"] = NEE_flag
        # NOTE: there is no need to force GPP to 0 when Fsd<threshold since
        #       OzFluxQC sets NEE=ER when Fsd<threshold.  Hence, the following
        #       lines are unecessary and have been commented out.
        # put the day time data into the GPP series
        #index = numpy.ma.where(Fsd>=Fsd_threshold)[0]
        #ds.series[output_label]["Data"][index] = GPP[index]
        #ds.series[output_label]["Flag"][index] = NEE_flag[index]
        # put the night time ER into the NEE series
        # This force nocturnal GPP to be 0!  Not sure this is the right thing to do.
        #index = numpy.ma.where(Fsd<Fsd_threshold)[0]
        #ds.series[output_label]["Data"][index] = numpy.float64(0)
        #ds.series[output_label]["Flag"][index] = numpy.int32(1)
        # copy the attributes
        attr = ds.series[output_label]["Attr"]
        attr["units"] = NEE_attr["units"]
        attr["long_name"] = "Gross Primary Productivity calculated as -1*"+NEE_label+"+"+ER_label

def rpGPP_createdict(cf,ds,series):
    """ Creates a dictionary in ds to hold information about calculating GPP."""
    # create the ffnet directory in the data structure
    if "gpp" not in dir(ds): ds.gpp = {}
    # create the dictionary keys for this series
    ds.gpp[series] = {}
    # output series name
    ds.gpp[series]["output"] = series
    # CO2 flux
    if "NEE" in cf["GPP"][series].keys():
        ds.gpp[series]["NEE"] = cf["GPP"][series]["NEE"]
    # ecosystem respiration
    if "ER" in cf["GPP"][series].keys():
        ds.gpp[series]["ER"] = cf["GPP"][series]["ER"]
    # create an empty series in ds if the output series doesn't exist yet
    if ds.gpp[series]["output"] not in ds.series.keys():
        data,flag,attr = qcutils.MakeEmptySeries(ds,ds.gpp[series]["output"])
        qcutils.CreateSeries(ds,ds.gpp[series]["output"],data,Flag=flag,Attr=attr)

def rpNEE_createdict(cf,ds,series):
    """ Creates a dictionary in ds to hold information about calculating NEE."""
    # create the ffnet directory in the data structure
    if "nee" not in dir(ds): ds.nee = {}
    # create the dictionary keys for this series
    ds.nee[series] = {}
    # output series name
    ds.nee[series]["output"] = series
    # CO2 flux
    if "Fc" in cf["NEE"][series].keys():
        ds.nee[series]["Fc"] = cf["NEE"][series]["Fc"]
    # ecosystem respiration
    if "ER" in cf["NEE"][series].keys():
        ds.nee[series]["ER"] = cf["NEE"][series]["ER"]
    # create an empty series in ds if the output series doesn't exist yet
    if ds.nee[series]["output"] not in ds.series.keys():
        data,flag,attr = qcutils.MakeEmptySeries(ds,ds.nee[series]["output"])
        qcutils.CreateSeries(ds,ds.nee[series]["output"],data,Flag=flag,Attr=attr)

def rpMerge_createdict(cf,ds,series):
    """ Creates a dictionary in ds to hold information about the merging of gap filled
        and tower data."""
    merge_prereq_list = []
    # get the section of the control file containing the series
    section = qcutils.get_cfsection(cf,series=series,mode="quiet")
    # create the ffnet directory in the data structure
    if "merge" not in dir(ds): ds.merge = {}
    # check to see if this series is in the "merge first" list
    # series in the "merge first" list get merged first so they can be used with existing tower
    # data to re-calculate Fg, Fn and Fa
    merge_order = "standard"
    if series in merge_prereq_list: merge_order = "prerequisite"
    if merge_order not in ds.merge.keys(): ds.merge[merge_order] = {}
    # create the dictionary keys for this series
    ds.merge[merge_order][series] = {}
    # output series name
    ds.merge[merge_order][series]["output"] = series
    # source
    ds.merge[merge_order][series]["source"] = ast.literal_eval(cf[section][series]["MergeSeries"]["Source"])
    # create an empty series in ds if the output series doesn't exist yet
    if ds.merge[merge_order][series]["output"] not in ds.series.keys():
        data,flag,attr = qcutils.MakeEmptySeries(ds,ds.merge[merge_order][series]["output"])
        qcutils.CreateSeries(ds,ds.merge[merge_order][series]["output"],data,Flag=flag,Attr=attr)
