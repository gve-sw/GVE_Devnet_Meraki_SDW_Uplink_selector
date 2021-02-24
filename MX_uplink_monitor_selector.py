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
import requests
import json
from mping import MultiPing, multi_ping
import time
import sys
from credentials import api_key, org_id


# ping_timeout and ping_retry usage:
# Using these parameters the code will ping the addresses up to ping_retry+1 times (initial ping + 3 retries), over the
# course of ping_timeout seconds.
# For example, if ping_timeout=.5 and ping_retry=0, for those addresses that do not
# respond another ping will be sent every 0.5 seconds.
# NOTE: never set the average_latency_tolerance less than or equal to ping_timeout otherwise you will not be able to accurately measure
# average latency since delayed packets will simply be reported as missing (loss)
ping_timeout=.5
ping_retry=0
# inter_ping_delay is the time to wait before invoking multi-ping. If all devices in the list reply to the ping quickly then
# there could potentially be a flurry of pings from this script to the various devices which could be detrimental or even raise
# alarms, so you can limit how often they are sent out. When there is packet loss and disconnected interfaces then the ping_timeout will
# add to the time between pings.
inter_ping_delay=0.5

# number of seconds to evaluate a negative network condition
trouble_eval_window = 20

# average latency in seconds to tolerate during the trouble_eval_window time period before deciding
# we have a latency problem
# NOTE: never set the average_latency_tolerance less than or equal to ping_timeout otherwise you will not be able to accurately measure
# average latency since delayed packets will simply be reported as missing (loss)
average_latency_tolerance=0.400

# For every time the ping library does not return a result it is because ping_retry+1 packets were sent within
# ping_timeout (seconds) and none came back.
# In the example above where ping_timeout=.5 and ping_retry=0, any missed results represents 1 packet within .5 seconds.
# That means that if we set trouble_eval_window to 20 seconds and period_loss_report_tolerance to 12
# we are detecting a packet loss of 30%. For more granularity on packet loss, reduce the ping_timeout and ping_retry
# values as well as the trouble_eval_window
period_loss_report_tolerance=12

# number of seconds after failing over to secondary WAN link to wait until evaluating main link again to switch back
failback_wait_time = 120

# set useWhiteList to True if you wish to only include devices from certain NetworkIds in the monitoring.
# to specify the list of network IDs to consider, add them one per line in the networks_whitelist.txt file in the same
# directory as this Python script. If the file is missing it will consider the whitelist as empty and not monitor
# any devices unless you set useWhiteList to False
useWhiteList=True

# If you wish to use the publicIP of the WAN interfaces instead of the
# IP assigned to the interface, set useWANpublicIP to True. This will extract the publicIP of the uplink
# (if available) using this API call https://developer.cisco.com/meraki/api/#!get-network-device-uplink
# and overwrite the IP address obtained for the MX devices
# using this API call https://developer.cisco.com/meraki/api/#!get-network-device ( wan1Ip and wan2Ip )
useWANpublicIP=False

# Assign one or more IP addresses as a strings in a list to scriptConnTestDestinations if you wish to have the script
# use ping destinations that are not one of the MX devices being evaluated
# to make sure the script has good network connectivity and it does not confuse network connectivity problems
# on the machine running the script with actual network issues at the sites where the MXs are installed
# If you are to use this option, it is suggested you use the IP addresses of DNS services
# such as Google ('8.8.8.8') , OpenDNS ('208.67.222.222') or any other that is very unlikely to stop responding.
# for example, to test Google and OpenDNS, configure scriptConnTestDestinations=['8.8.8.8','208.67.222.222'],
# for just Google DNS, then scriptConnTestDestinations=['8.8.8.8']. Leave as an empty list (scriptConnTestDestinations=[])
# if you do not wish to use this option
scriptConnTestDestinations=[]




dashboard = meraki.DashboardAPI(api_key, output_log=False, suppress_logging= True)

# isTestConnDown is a boolean used to indicate if the test connection is healthy or not IF scriptConnTestDestinations
# is configured.
isTestConnDown= {}

class WAN_device:
    global trouble_eval_window, average_latency_tolerance, period_loss_report_tolerance, failback_wait_time, isTestConnDown

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
        self.lat2_reports=[]
        self.loss2_reports=[]

    def __repr__(self):
        return(f'NetworkId: {self.networkId}, Serial: {self.serial}, Org number: {self.my_org_number}')

    def uplink_selector(self, ulinksLatency):
        # current box latency for both WAN1 and WAN2 are passed in via 2 element array ulinksLatency
        # ulinksLatency[0] contains latency measure for WAN1
        # ulinksLatency[1] contains latency measure for WAN2
        # the measure can be one of these three:
        #   Float : latency as measured by a ping from where this script is running to the Meraki MX uplink interface
        #    -1 : interface is unreachable or disconnected, it is also used to estimate packet loss
        #    None : interface is not configured in the Meraki Dashboard for that MX device
        global isTestConnDown

        #first let's grab a current timestamp to use in all operations
        current_time=time.time()

        # check for the existence of WAN1 also if it is responding, no point in switching back to it
        # if not configured or disconnected!!
        bActiveWAN1 = not (ulinksLatency[0] == None or ulinksLatency[0] == -1)

        # check for the existence of WAN2 also if it is responding, no point in switching to it
        # if not configured or disconnected!!
        bActiveWAN2=not (ulinksLatency[1]==None or ulinksLatency[1]==-1)



        # now check for the existence of a WAN1 uplink (otherwise do nothing)
        if (ulinksLatency[0] != None):
            # now let's add to the queues containing the latency or loss reports correspondingly
            if ulinksLatency[0]>=0:
                self.lat1_reports.append([current_time,ulinksLatency[0]])
            else:
                self.loss1_reports.append(current_time)

            # now we need to remove any reports that are outside the trouble_eval_window
            while len(self.lat1_reports)>0 and current_time-self.lat1_reports[0][0] > trouble_eval_window:
                throwaway=self.lat1_reports.pop(0)

            while len(self.loss1_reports)>0 and current_time-self.loss1_reports[0] > trouble_eval_window:
                throwaway=self.loss1_reports.pop(0)

            # now we want to also keep stats for WAN2, so need to make sure the data structure
            # exists and do just like we did with WAN1
            if (ulinksLatency[1] != None):
                # now let's add to the queues containing the latency or loss reports correspondingly
                if ulinksLatency[1] >= 0:
                    self.lat2_reports.append([current_time, ulinksLatency[1]])
                else:
                    self.loss2_reports.append(current_time)

                # now we need to remove any reports that are outside the trouble_eval_window
                while len(self.lat2_reports) > 0 and current_time - self.lat2_reports[0][0] > trouble_eval_window:
                    throwaway = self.lat2_reports.pop(0)

                while len(self.loss2_reports) > 0 and current_time - self.loss2_reports[0] > trouble_eval_window:
                    throwaway = self.loss2_reports.pop(0)

            #check to see if we are within the initial eval window to start running the logic
            if current_time-self.init_time>=trouble_eval_window:
                #first calculate the average latency time, if any (could be all loss packet reports) for WAN1
                average_latency1=0
                if len(self.lat1_reports)>0:
                    lat_sum1=0
                    for lat1_rep in self.lat1_reports:
                        lat_sum1+=lat1_rep[1]
                    average_latency1 = lat_sum1/len(self.lat1_reports)

                # Now for WAN2
                average_latency2=0
                if len(self.lat2_reports)>0:
                    lat_sum2=0
                    for lat2_rep in self.lat2_reports:
                        lat_sum2+=lat2_rep[1]
                    average_latency2 = lat_sum2/len(self.lat2_reports)

                #next, get the number of loss reports, if any (could have had no packet loss in period)
                loss_count1=len(self.loss1_reports)
                loss_count2=len(self.loss2_reports)


                print(self.serial," Average latency1: ",average_latency1," Loss count1: ",loss_count1)
                print(self.serial," Average latency2: ",average_latency2," Loss count2: ",loss_count2)


                if self.serial[0 : 6]=='tester':
                    #handling for special object with serial 'tester' to decide if we proceed with logic
                    if self.current_uplink==1 and (average_latency1>average_latency_tolerance or loss_count1>period_loss_report_tolerance):
                        print("lat1_reports: ", self.lat1_reports)
                        print("loss1_reports: ", self.loss1_reports)

                        # sets global object to stop checking the rest of MX devices!!!
                        isTestConnDown[self.uplink1_ip]=True

                        #keep setting the "current_uplink" for consistency, but not needed for this type of object
                        self.current_uplink = 2
                        self.last_failover_time = current_time
                        print('tester '+self.serial+' experiencing problems; marking as such in list')
                    else:
                        if self.current_uplink == 2 and current_time - self.last_failover_time > failback_wait_time:
                            print(
                                "Two minutes have passed since tester "+self.serial+" went bad, check to see if now ok to mark as such...")
                            if average_latency1 <= average_latency_tolerance and loss_count1 <= period_loss_report_tolerance:
                                print("lat1_reports: ", self.lat1_reports)
                                print("loss1_reports: ", self.loss1_reports)

                                #set global object to continue checking the rest of MX devices!!!
                                isTestConnDown[self.uplink1_ip]=False
                                self.current_uplink = 1
                                print('tester '+self.serial+' back up after failback wait time.. marking as such in list')


                elif len(isTestConnDown) == 0 or not all(isTestConnDown.values()):

                    bUnstableWAN1=average_latency1>average_latency_tolerance or loss_count1>period_loss_report_tolerance
                    bUnstableWAN2=average_latency2>average_latency_tolerance or loss_count2>period_loss_report_tolerance
                    print("bUnstableWAN1:",bUnstableWAN1, " bUnstableWAN2:",bUnstableWAN2)


                    # ready to check to see if we have to make any uplink changes
                    # first, and only if we are currently on uplink 1 (WAN1), check to see if it has been problematic during
                    # the last seconds specified in trouble_eval_window and see if we need to switch to uplink2 (WAN2)
                    if self.current_uplink==1 and bUnstableWAN1 and bActiveWAN2 and not bUnstableWAN2:
                        print("lat1_reports: ", self.lat1_reports)
                        print("loss1_reports: ", self.loss1_reports)
                        # Set WAN2 as uplink on device, turn off load balancing and record the time we failed over
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
                            if bActiveWAN1 and not bUnstableWAN1:
                                print("lat1_reports: ", self.lat1_reports)
                                print("loss1_reports: ", self.loss1_reports)
                                dashboard.appliance.updateNetworkApplianceTrafficShapingUplinkSelection(
                                    networkId=self.networkId, loadBalancingEnabled=True, defaultUplink='wan1')
                                # since we called the Meraki Dashboard API withing a MX Device Object method which is called within a large loop
                                # we need to guarantee that we do not call the API more than 5 times per second so if we add a .2 sec delay
                                # here , even if many other objects will have to make a WAN change, we will not be calling more than 5 times a second
                                time.sleep(.20)
                                self.current_uplink = 1
                                print('WAN1 good after failback wait time: Failing back to WAN1 as uplink, Load Balancing enabled')



allMXDevices={}
deviceSerialofUplinkIP={}
allUplinkIPs=[]
def refreshDevicesDict():
    global allMXDevices, allUplinkIPs, useWhiteList, scriptConnTestDestination
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


    # If scriptConnTestDestinations is not empty, add them as the first "MX devices" with a serial number that
    # identifies them as a special test destination "device" to include in ping test but not consider for
    # switchover
    if len(scriptConnTestDestinations)>0:
        for testerIP in scriptConnTestDestinations:
            testerSString='tester'+testerIP
            allMXDevices[testerSString] = WAN_device(networkId=testerSString, serial=testerSString, uplink1_ip=testerIP, uplink2_ip='', my_org_number=org_id)
            deviceSerialofUplinkIP[testerIP] = [testerSString, 'wan1']
            allUplinkIPs.append(testerIP)
            isTestConnDown[testerIP]=False

    # Get the last 5 minutes of UplinkLoss and Latency data for all MX devices in the Organization
    # to make a list of which to monitor via Ping.
    org = dashboard.organizations.getOrganizationDevicesUplinksLossAndLatency(organizationId=org_id)
    print('updating devices')
    for anEntry in org:
        if anEntry['serial'] not in allMXDevices.keys():
            deviceInfo=dashboard.devices.getDevice(anEntry['serial'])
            print("GetDevice: ",deviceInfo)
            print("Trying to call getNetworkDeviceUplink with: ",anEntry['networkId'],"  ",anEntry['serial'])
            url = "https://api.meraki.com/api/v0/networks/"+anEntry['networkId']+"/devices/"+anEntry['serial']+"/uplink"
            payload = None
            headers = {
                "Content-Type": "application/json",
                "Accept": "application/json",
                "X-Cisco-Meraki-API-Key": api_key
            }
            response = requests.request('GET', url, headers=headers, data=payload)
            deviceULinkInfo=json.loads(response.text.encode('utf8'))
            print("DeviceULinkInfo: ", deviceULinkInfo)

            # If useWhiteList is True, then there the NetworkId of the device has to be in the list for it to be considered.
            # Otherwise, the condition will always be met and the device will be considered to add to the list.
            if ((not useWhiteList)  or  (anEntry['networkId'] in white_list)):
                #fist make sure this device is not a warm spare using getNetworkApplianceWarmSpare call which returns:
                #{
                #     "enabled": false,
                #     "primarySerial": "Q2BN-6Q6Z-RTR4",
                #     "spareSerial": null
                # }
                response_spare = dashboard.appliance.getNetworkApplianceWarmSpare(anEntry['networkId'])
                #print("Evaluating warm spare for ",anEntry['serial'],": ",response_spare)
                if response_spare['primarySerial']==anEntry['serial']:
                    wan1IP=deviceInfo['wan1Ip']
                    wan2IP=deviceInfo['wan2Ip']

                    #  Change to the publicIp of an uplink if useWANpublicIP is set to true
                    if useWANpublicIP:
                        if len(deviceULinkInfo)>0:
                            if deviceULinkInfo[0]['interface']=='WAN 1':
                                wan1IP=deviceULinkInfo[0]['publicIp']
                            elif deviceULinkInfo[0]['interface']=='WAN 2':
                                wan2IP = deviceULinkInfo[0]['publicIp']
                        if len(deviceULinkInfo)>1:
                            if deviceULinkInfo[1]['interface']=='WAN 1':
                                wan1IP=deviceULinkInfo[1]['publicIp']
                            elif deviceULinkInfo[1]['interface']=='WAN 2':
                                wan2IP = deviceULinkInfo[1]['publicIp']


                    allMXDevices[anEntry['serial']] = WAN_device(networkId=anEntry['networkId'], serial=anEntry['serial'],uplink1_ip=wan1IP,uplink2_ip=wan2IP, my_org_number=org_id)
                    #keeping track of which IPs belong to which MX devices and also which wan link is for each IP address
                    if wan1IP!=None:
                        deviceSerialofUplinkIP[wan1IP]=[anEntry['serial'],'wan1']
                        allUplinkIPs.append(wan1IP)
                    if wan2IP!=None:
                        deviceSerialofUplinkIP[wan2IP]=[anEntry['serial'],'wan2']
                        allUplinkIPs.append(wan2IP)

refreshDevicesDict()
print(allMXDevices)


# forever loop to ping all devices and decide if to act
while True:
    if len(allUplinkIPs)>0:
        responses, no_responses = multi_ping(allUplinkIPs, timeout=ping_timeout, retry=ping_retry, ignore_lookup_errors=True)
        print("responses=", responses, "no_responses=", no_responses)

        responsesPerSerial={}
        # example responsesPerSerial['ER34234']=[0.009306907653808594,0.012850046157836914]
        for response in responses.keys():
            theDev=deviceSerialofUplinkIP[response]
            theDevSerial=theDev[0]
            theDevWan = theDev[1]
            # initialize the response array if not already done
            if theDevSerial not in responsesPerSerial:
                responsesPerSerial[theDevSerial]=[None,None]
            if theDevWan=='wan1':
                responsesPerSerial[theDevSerial][0]=responses[response]
            if theDevWan=='wan2':
                responsesPerSerial[theDevSerial][1]=responses[response]

        #now process the no_response array
        for nresponse in no_responses:
            theDev=deviceSerialofUplinkIP[nresponse]
            theDevSerial=theDev[0]
            theDevWan = theDev[1]
            #initialize the response array if there was none returned in response above
            if theDevSerial not in responsesPerSerial:
                responsesPerSerial[theDevSerial] = [None, None]
            if theDevWan=='wan1':
                responsesPerSerial[theDevSerial][0]=-1
            if theDevWan=='wan2':
                responsesPerSerial[theDevSerial][1]=-1


        for entry_serial in allMXDevices:
            allMXDevices[entry_serial].uplink_selector(responsesPerSerial[entry_serial])

        #just to give a small break between calls to multi-ping, could remove
        time.sleep(inter_ping_delay)
    else:
        print("No devices to ping...")
        # sleep for a minute in case they want to keep it running to arrive at the top of the hour to check again
        # for devices
        time.sleep(60)

    #check for new devices at the top of the hour
    if ((time.time() % 3600) == 0):
        refreshDevicesDict()