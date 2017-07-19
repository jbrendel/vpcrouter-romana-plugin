"""
Copyright 2017 Pani Networks Inc.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.

"""

#
# A VPC router watcher plugin, which observes the topology information
# maintained by Romana 2.0 in etcd.
#

import etcd3
import json
import logging
import threading
import time

from vpcrouter         import utils
from vpcrouter.errors  import ArgsError
from vpcrouter.watcher import common


class Romana(common.WatcherPlugin):
    """
    Implements the WatcherPlugin interface for the 'romana' plugin.

    """
    def __init__(self, *args, **kwargs):
        self.key                = "/romana/ipam/data"
        self.connect_check_time = kwargs.pop('connect_check_time', 2)
        self.etcd_timeout_time  = kwargs.pop('etcd_timeout_time', 2)
        self.keep_running       = True
        self.watch_id           = None
        self.etcd               = None
        super(Romana, self).__init__(*args, **kwargs)

    def load_topology_send_route_spec(self):
        """
        Retrieve latest topology info from Romana topology store and send
        new spec.

        """
        try:
            d = json.loads(self.etcd.get(self.key)[0])
            route_spec = {}
            for net_name, net_data in d['networks'].items():
                cidr  = net_data['groups']['cidr']
                hosts = [h['ip'] for h in net_data['groups']['hosts']]
                route_spec[cidr] = hosts
            common.parse_route_spec_config(route_spec)
            self.q_route_spec.put(route_spec)

        except Exception as e:
            logging.error("Cannot load Romana topology data at '%s': %s" %
                          (self.key, str(e)))

    def event_callback(self, event):
        """
        Event handler function for watch on Romana IPAM data.

        This is called whenever there is an update to that data detected.

        """
        logging.info("Romana watcher plugin: Detected topology change in "
                     "Romana topology data")
        self.load_topology_send_route_spec()

    def etcd_check_status(self):
        """
        Check the status of the etcd connection.

        Return False if there are any issues.

        """
        if self.etcd:
            try:
                self.etcd.status()
                return True
            except Exception as e:
                logging.debug("Cannot get status from etcd: %s" % str(e))
        else:
            logging.debug("Cannot get status from etcd, no connection")

        return False

    def establish_etcd_connection_and_watch(self):
        """
        Get connection to ectd and install a watch for Romana topology data.

        """
        if not self.etcd or not self.etcd_check_status() or \
                                                self.watch_id is None:
            try:
                logging.debug("Attempting to connect to etcd")
                self.etcd = etcd3.client(host=self.conf['addr'],
                                         port=int(self.conf['port']),
                                         timeout=self.etcd_timeout_time)

                logging.debug("Initial data read")
                self.load_topology_send_route_spec()

                logging.debug("Attempting to establish watch on '%s'" %
                              self.key)
                self.watch_id = self.etcd.add_watch_callback(
                                        self.key, self.event_callback)

                logging.info("Romana watcher plugin: Established etcd "
                             "connection and watch for topology data")
            except Exception as e:
                logging.error("Cannot establish connection to etcd: %s" %
                              str(e))
                self.etcd     = None
                self.watch_id = None

    def watch_etcd(self):
        """
        Start etcd connection, establish watch and do initial read of data.

        Regularly re-checks the status of the connection. In case of problems,
        re-establishes a new connection and watch.

        """
        while self.keep_running:
            self.etcd     = None
            self.watch_id = None

            self.establish_etcd_connection_and_watch()

            # Slowly loop as long as the connection status is fine.
            while self.etcd_check_status() and self.keep_running:
                time.sleep(self.connect_check_time)

            logging.warning("Romana watcher plugin: Lost etcd connection.")

    def start(self):
        """
        Start the configfile change monitoring thread.

        """
        logging.info("Romana watcher plugin: "
                     "Starting to watch for topology updates...")
        self.observer_thread = threading.Thread(target = self.watch_etcd,
                                                name   = "RomanaMon",
                                                kwargs = {})

        self.observer_thread.daemon = True
        self.observer_thread.start()

    def stop(self):
        """
        Stop the config change monitoring thread.

        """
        if self.watch_id:
            self.etcd.cancel_watch(self.watch_id)
        logging.debug("Sending stop signal to etcd watcher thread")
        self.keep_running = False
        self.observer_thread.join()
        logging.info("Romana watcher plugin: Stopped")

    @classmethod
    def add_arguments(cls, parser):
        """
        Add arguments for the Romana mode to the argument parser.

        """
        parser.add_argument('-a', '--address', dest="addr",
                            default="localhost",
                            help="etcd's address to connect to "
                                 "(only in Romana mode, default: localhost)")
        parser.add_argument('-p', '--port', dest="port",
                            default="2379", type=int,
                            help="etcd's port to connect to "
                                 "(only in Romana mode, default: 2379)")
        return ["addr", "port"]

    @classmethod
    def check_arguments(cls, conf):
        """
        Sanity check options needed for Romana mode.

        """
        if not 0 < conf['port'] < 65535:
            raise ArgsError("Invalid etcd port '%d' for Romana mode." %
                            conf['port'])
        if not conf['addr'] == "localhost":
            # Check if a proper address was specified
            utils.ip_check(conf['addr'])
