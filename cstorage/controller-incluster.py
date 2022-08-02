#!/usr/bin/env python
"""A controller """

import json
import logging
import os
import time
import httplib
import sys
import requests

from kubernetes import client, config
from kubernetes.client.rest import ApiException
import crd

from multiprocessing import Process
from google.cloud import pubsub_v1

GROUP = "kubeless.io"
VERSION = "v1beta1"
PLURAL = "cloudstoragetriggers"

config.load_incluster_config()

v1 = client.CoreV1Api()
crds = client.CustomObjectsApi()

class Trigger(object):
    def __init__(self, obj):
        self._obj = obj
        self._apiversion = obj["apiVersion"]
        self._kind = obj["kind"]
        self._metadata = obj["metadata"]
        self._spec = obj["spec"]
        
    def crd_name(self):
        return self._metadata["name"]
            
    def any_versions(self):
        return f"name={self.crd_name()}"
    
    def subscription(self):
        return self._spec["subscription"]
        
    def project(self):
        return self._spec["project"]
        
    def functions(self):
        return self._spec["functionSelector"]["matchLabels"]

def get_mappings():
    try: 
        mappings = crds.list_cluster_custom_object(GROUP, VERSION, PLURAL)
    except ApiException as e:
        print("Exception when calling CustomObjectsApi->list_cluster_custom_object: %s\n" % e)
    return mappings['items']

def get_functions(selector):
    try: 
        functions = crds.list_cluster_custom_object(GROUP, VERSION, 'functions', label_selector=selector)
    except ApiException as e:
        print("Exception when calling CustomObjectsApi->list_cluster_custom_object: %s\n" % e)
    return functions['items']
    
def create_selector(func_selectors):
    selector = [f'{keys}={func_selectors[keys]}' for keys in func_selectors.keys()]
    return ",".join(selector)

def cloudstorage(subscription, project, func_selectors):

    def callback(message):
        sys.stdout = open(f"{str(os.getpid())}.out", "a", buffering=0)
        sys.stderr = open(f"{str(os.getpid())}_error.out", "a", buffering=0)
        sys.stdout = open(str(os.getpid()) + ".out", "a", buffering=0)
        # TODO: Add error handling for svc selection and post requests

        services = v1.list_service_for_all_namespaces(label_selector=create_selector(func_selectors))
        for svc in services.items:
            svc_url = f'http://{svc.metadata.name}.{svc.metadata.namespace}:{str(svc.spec.ports[0].port)}'

            # use line below when running in-cluster
            requests.post(svc_url, data=message.data)
                    # remove line below when running in-cluster
                    #requests.post('http://192.168.99.100:31352', data= message.data)
        message.ack()

    sys.stdout = open(f"{str(os.getpid())}.out", "a", buffering=0)
    sys.stderr = open(f"{str(os.getpid())}_error.out", "a", buffering=0)

    subscriber = pubsub_v1.SubscriberClient()
    subscription_path = subscriber.subscription_path(project, subscription)
    subscriber.subscribe(subscription_path, callback=callback)

    while True:
        time.sleep(60)

def create_meta(trigger):
    subscription = trigger.subscription()
    functions = trigger.functions()
    project = trigger.project()
    p = Process(target=cloudstorage, args=(subscription, project, functions,))
    p.start()
    return p
        
def update_meta(trigger):
    try:
        p = create_meta(trigger)
    except ApiException as e:
        if e.status != httplib.CONFLICT:
            raise e
    return p
        
def delete_meta(name, pid):
    for p in pid:
        logging.warning(f"check name {p[0]} and pid {str(p[1])}")
        if p[0] == name:
            p[1].terminate()
            p[1].join()
    logging.warning("Deleted the CloudStorage Trigger")

def process_meta(t, trigger, pid):
    if t == "DELETED":
        delete_meta(trigger.crd_name(), pid)
        logging.warning("Deleted CRD, check garbage collection")
    elif t in ["MODIFIED", "ADDED"]:
        p = update_meta(trigger)
        pid.append((trigger.crd_name(), p))
    else:
        logging.error("Unrecognized event type: %s", t)
    
def main():
    
    pid = []
    
    # TODO: create initialization step
    for mapping in get_mappings():
        process_meta("ADDED", Trigger(mapping), pid)
    
    resource_version = ""
    while True:
        try:
            stream = crd.Watch().stream(crds.list_cluster_custom_object,
                                          GROUP, VERSION, PLURAL,
                                          resource_version=resource_version)
        except ApiException as e:
            print "Exception when calling CustomObjectsApi->list_cluster_custom_object: %s\n" % e
                       
        for event in stream:
            try:
                t = event["type"]
                obj = event["object"]
                trigger = Trigger(obj)
                logging.warning("Trigger %s, %s" % (trigger.crd_name(),t))  
                process_meta(t, trigger, pid)

                # Configure where to resume streaming.
                metadata = obj.get("metadata")
                if metadata:
                    resource_version = metadata['resourceVersion']
                    #resource_version = crds.list_cluster_custom_object(GROUP, VERSION, PLURAL)["metadata"]["resourceVersion"]
            except:
                logging.exception("Error handling event")

if __name__ == "__main__":
    main()
