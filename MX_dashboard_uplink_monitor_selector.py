"""
Copyright (c) 2020 Cisco and/or its affiliates.

This software is licensed to you under the terms of the Cisco Sample
Code License, Version 1.1 (the "License"). You may obtain a copy of the
License at

               https://developer.cisco.com/docs/licenses

All use of the material herein must be in accordance with the terms of
the License. All rights not expressly granted by the License are
reserved. Unless required by applicable law or agreed to separately in
writing, software distributed under the License is distributed on an "AS
IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express
or implied.
"""

import meraki
import time
import sys
from credentials import api_key, org_id
from datetime import datetime

#dasboard_call_delay is the number of seconds to wait between calls to the Meraki Dashboard to evaluate the condition of
# uplinks. This can be as little as .20 seconds, but that would be the limit of API calls per second an application can make.
# default is set to 1 seconds so that the script can get the updated statistics at most 1 second after they are available,
# giving us visibility in to stats starting at 121 seconds in the past.
dashboard_call_delay=1

# number of seconds to evaluate a negative network condition. This window starts from T-120 seconds and into the past given that the
# Meraki Dashboard API does not provide any stats earlier than that.
trouble_eval_window = 60

# average latency in seconds to tolerate during the trouble_eval_window time period before deciding
# we have a latency problem
# NOTE: never set the average_latency_tolerance less than or equal to ping_timeout otherwise you will not be able to accurately measure
# average latency since delayed packets will simply be reported as missing (loss)
average_latency_tolerance=0.400


# average loss in percent to tolerate during the trouble_eval_window time period before deciding
# we have a loss problem
average_loss_tolerance=30



# number of seconds after failing over to secondary WAN link to wait until evaluating main link again to switch back
failback_wait_time = 240

# set useWhiteList to True if you wish to only include devices from certain NetworkIds in the monitoring.
# to specify the list of network IDs to consider, add them one per line in the networks_whitelist.txt file in the same
# directory as this Python script. If the file is missing it will consider the whitelist as empty and not monitor
# any devices unless you set useWhiteList to False
useWhiteList=True


dashboard = meraki.DashboardAPI(api_key, output_log=False, suppress_logging= True)


class WAN_device:
    global trouble_eval_window, average_latency_tolerance, average_loss_tolerance, failback_wait_time

    def __init__(self, networkId, serial, my_org_number, uplink1_ip, uplink2_ip):
        self.networkId = networkId
        self.serial = serial
        self.my_org_number = my_org_number
        self.uplink1_ip = uplink1_ip
        self.uplink2_ip = uplink2_ip
        self.current_uplink = 1
        self.loss1=0
        self.loss2=0
        self.latency1=0
        self.latency2=0
        self.last_failover_time=0
        self.init_time=time.time()
        self.lat1_reports=[]
        self.loss1_reports=[]

    def __repr__(self):
        return(f'NetworkId: {self.networkId}, Serial: {self.serial}, Org number: {self.my_org_number}')

    def uplink_selector(self, ulinksLatency):
        # current box stats for both WAN1 and WAN2 are passed in via 2 element array ulinksLatency
        # ulinksLatency[0] contains timeseries with Loss and Latency for WAN1
        # ulinksLatency[1] contains timeseries with Loss and Latency for WAN2
        # the measure can be one of these three:
        #    None : no measurements where returned by the dashboard this time
        #   Array of dicts :   each dict corresponds to a measurement at a timestamp
        # example ulinksLatency[0]:
        #   [
        #             {
        #                 "ts": "2020-12-21T15:40:32Z",
        #                 "lossPercent": 0,
        #                 "latencyMs": 16.4
        #             },
        #             {
        #                 "ts": "2020-12-21T15:41:31Z",
        #                 "lossPercent": 0,
        #                 "latencyMs": 16.6
        #             }
        #     ]


        #first let's grab a current timestamp to use in all operations (UTC)
        current_time=datetime.utcnow().timestamp()


        # now check for the existence of a WAN1 uplink (otherwise do nothing)
        if (ulinksLatency[0] != None):
            # now let's make the calculations to see if the data contained in ulinksLatency[0]
            # indicates average latency in the trouble_eval_window period is greater than average_latency_tolerance or if
            # the average loss is greater than average_loss_tolerance percent

            # 1- evaluate all timeseries entries in ulinksLatency[0] and calculate average loss and latency for the latest trouble_eval_window seconds

            # These are just variuos datetime calculations we are going to need
            #max_lat_dt = datetime.strptime(max_lat_ts, '%Y-%m-%dT%H:%M:%SZ')
            #current_dt = datetime.fromtimestamp(current_time)
            #no_microseconds_time = time.mktime(current_dt.timetuple())
            #dt_string= current_dt.strftime('%Y-%m-%dT%H:%M:%SZ')


            failover_latency={
                'cumulative':0,
                'counts':0,
                'max_ts': current_time-10000,
                'min_ts': current_time,
                'average':0
            }
            failover_loss={
                'cumulative':0,
                'counts':0,
                'max_ts': current_time-10000,
                'min_ts': current_time,
                'average':0
            }



            for tsEntry in ulinksLatency[0]:
                entry_timestamp_dt = datetime.strptime(tsEntry['ts'], '%Y-%m-%dT%H:%M:%SZ')
                entry_timestamp = entry_timestamp_dt.timestamp()

                # print("tabulatings for ", self.serial)
                # print("Entry: ",tsEntry)
                # print("current_time ",current_time)
                # print("entry_timestamp ",entry_timestamp)
                # print("trouble_eval_window ",trouble_eval_window)



                if 'lossPercent' in tsEntry and tsEntry['lossPercent']!=None:
                    if ((current_time - entry_timestamp)>=120) and ((current_time - entry_timestamp)<(120 + trouble_eval_window)):
                        failover_loss['cumulative']+=tsEntry['lossPercent']
                        failover_loss['counts']+=1
                        if entry_timestamp<failover_loss['min_ts']:
                            failover_loss['min_ts']=entry_timestamp
                        if entry_timestamp>failover_loss['max_ts']:
                            failover_loss['max_ts']=entry_timestamp
                if 'latencyMs' in tsEntry and tsEntry['latencyMs']!=None:
                    if ((current_time - entry_timestamp)>=120) and ((current_time - entry_timestamp)<(120 + trouble_eval_window)):
                        failover_latency['cumulative']+=tsEntry['latencyMs']/1000
                        failover_latency['counts']+=1
                        if entry_timestamp<failover_latency['min_ts']:
                            failover_latency['min_ts']=entry_timestamp
                        if entry_timestamp>failover_latency['max_ts']:
                            failover_latency['max_ts']=entry_timestamp

            if failover_latency['counts']>0:
                failover_latency['average']=failover_latency['cumulative']/failover_latency['counts']
            if failover_loss['counts']>0:
                failover_loss['average']=failover_loss['cumulative']/failover_loss['counts']

            print("Evaluating ",self.serial)
            print("Failover latency: ", failover_latency)
            print("Failover loss: ", failover_loss)


            # check for the existence of WAN2 also if it is responding, no point in switching to it
            # if not configured or disconnected!!
            #first check for empty data structure
            bActiveWAN2 = not (ulinksLatency[1] == None)
            #then to see if any of the stats come back null
            if bActiveWAN2:
                for tsEntry in ulinksLatency[1]:
                    if 'lossPercent' in tsEntry:
                        if tsEntry['lossPercent']==None or tsEntry['lossPercent']==100:
                            bActiveWAN2=False
                    if 'latencyMs' in tsEntry:
                        if tsEntry['latencyMs'] == None:
                            bActiveWAN2 = False

            print("bActiveWAN2 is ",bActiveWAN2)
            # ready to check to see if we have to make any uplink changes
            # first, and only if we are currently on uplink 1 (WAN1), check to see if it has been problematic during
            # the last seconds specified in trouble_eval_window and see if we need to switch to uplink2 (WAN2)
            if self.current_uplink==1 and bActiveWAN2 and (failover_latency['average']>average_latency_tolerance or failover_loss['average']>average_loss_tolerance):

                # if WAN2 exists and have problems with WAN1, set it as uplink on device, turn off load balancing and record the time we failed over
                dashboard.appliance.updateNetworkApplianceTrafficShapingUplinkSelection(networkId=self.networkId, loadBalancingEnabled=False, defaultUplink='wan2')
                # since we called the Meraki Dashboard API withing a MX Device Object method which is called within a large loop
                # we need to guarantee that we do not call the API more than 5 times per second so if we add a .2 sec delay
                # here , even if many other objects will have to make a WAN change, we will not be calling more than 5 times a second
                time.sleep(.20)
                self.current_uplink = 2
                self.last_failover_time=current_time
                print('WAN1 problems after tolerance period: Load Balancing disabled, using WAN2 as uplink')
            else:
                if self.current_uplink==2 and current_time-self.last_failover_time>failback_wait_time:
                    # since enough time has passed since failover to WAN2, and WAN1 seems to have been healthy for the past
                    # number of seconds specified by trouble_eval_window, it is safe to fail back to WAN1 and turn
                    # on load balancing.
                    print("Two minutes have passed since failover, check to see if WAN1 is ok to switch back...")
                    if failover_latency['average']<=average_latency_tolerance and failover_loss['average']<=average_loss_tolerance:
                        dashboard.appliance.updateNetworkApplianceTrafficShapingUplinkSelection(
                            networkId=self.networkId, loadBalancingEnabled=True, defaultUplink='wan1')
                        # since we called the Meraki Dashboard API withing a MX Device Object method which is called within a large loop
                        # we need to guarantee that we do not call the API more than 5 times per second so if we add a .2 sec delay
                        # here , even if many other objects will have to make a WAN change, we will not be calling more than 5 times a second
                        time.sleep(.20)
                        self.current_uplink = 1
                        print('WAN1 good after failback wait time: Failing back to WAN1 as uplink, Load Balancing enabled')



allMXDevices={}
responsesPerSerial = {}
# example responsesPerSerial['ER34234']=
#   [
#             {
#                 "ts": "2020-12-21T15:40:32Z",
#                 "lossPercent": 0,
#                 "latencyMs": 16.4
#             },
#             {
#                 "ts": "2020-12-21T15:41:31Z",
#                 "lossPercent": 0,
#                 "latencyMs": 16.6
#             }
#     ]
#,
# [
#             {
#                 "ts": "2019-01-31T18:46:13Z",
#                 "lossPercent": 5.3,
#                 "latencyMs": 194.9
#             }
#  ]


def refreshDevicesDict():
    global allMXDevices, allUplinkIPs, useWhiteList, responsesPerSerial
    allUplinkIPs=[]
    allMXDevices = {}
    white_list=[]

    # read a whitelist of network IDs to consider when adding devices to the Dict
    try:
        with open('networks_whitelist.txt') as my_file:
            white_list = my_file.read().splitlines()
    except IOError as e:
        print("Error trying to read whitelist, skipping...")
    except:
        print("Unexpected error: ",sys.exc_info()[0])

    # Get the last 5 minutes of UplinkLoss and Latency data for all MX devices in the Organization
    # to make a list of which to monitor
    org = dashboard.organizations.getOrganizationDevicesUplinksLossAndLatency(organizationId=org_id)
    print('updating devices')
    for anEntry in org:
        if anEntry['serial'] not in allMXDevices.keys():
            deviceInfo=dashboard.devices.getDevice(anEntry['serial'])

            # If useWhiteList is True, then there the NetworkId of the device has to be in the list for it to be considered.
            # Otherwise, the condition will always be met and the device will be considered to add to the list.
            if ((not useWhiteList)  or  (anEntry['networkId'] in white_list)):
                wan1IP=deviceInfo['wan1Ip']
                wan2IP=deviceInfo['wan2Ip']
                allMXDevices[anEntry['serial']] = WAN_device(networkId=anEntry['networkId'], serial=anEntry['serial'],uplink1_ip=wan1IP,uplink2_ip=wan2IP, my_org_number=org_id)
                responsesPerSerial[anEntry['serial']] = [None, None]

refreshDevicesDict()
print(allMXDevices)


# forever read stats for all devices and decide if to act
while True:
    org = dashboard.organizations.getOrganizationDevicesUplinksLossAndLatency(organizationId=org_id)

    for anEntry in org:
        # assemble the responsesPerSerial{} for each device
        if anEntry['serial']  in allMXDevices.keys():
            if anEntry['uplink']=="wan1":
                responsesPerSerial[anEntry['serial']][0] = anEntry['timeSeries']
            if anEntry['uplink']=="wan2":
                responsesPerSerial[anEntry['serial']][1] = anEntry['timeSeries']


    #now that we a response per device with WAN1 and WAN2, evaluate the switching of uplinks by
    #callign the objects uplink_selector() method
    for entry_serial in allMXDevices:
        allMXDevices[entry_serial].uplink_selector(responsesPerSerial[entry_serial])
        responsesPerSerial[entry_serial] = [None, None]

    #pause so we are not calling the dashboard continuosly
    time.sleep(dashboard_call_delay)

    #check for new devices at the top of the hour
    if ((time.time() % 3600) == 0):
        refreshDevicesDict()