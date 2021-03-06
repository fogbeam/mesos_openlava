"""
OpenLava master run as the framework.
"""

import logging
import json
import os
import sys
import uuid

from mesos import native
from mesos import interface
from mesos.interface import mesos_pb2

import util
import ui.web

__author__ = 'tmetsch'

LOG = logging.getLogger(__name__)


class OpenLavaScheduler(interface.Scheduler):
    """
    OpenLava Mesos scheduler.
    """

    def __init__(self, executor):
        self.executor = executor
        self.accepted_tasks = {}
        self.running_tasks = {}

        self.master_host = util.start_lava(is_master=True)
        ui.web.serve()
        self.master_ip = util.get_ip(self.master_host)

    def resourceOffers(self, driver, offers):
        """
        Apache Mesos invokes this to inform us about offers. We can accept
        or decline...
        """
        # TODO: let's become smarter and grab only what we need in \
        #       future. - match pending jobs in queues to offers from mesos.
        # TODO: candidate: https://github.com/Netflix/Fenzo
        for offer in offers:
            # not going to run openlava twice on same hosts.
            if str(offer.hostname) in self.accepted_tasks:
                # TODO: could tune number of job slots available on that host.
                # TODO: work with filters
                driver.declineOffer(offer.id)

            # if prio queue > 10 exclusively assign offers to prio queue.
            if util.get_queue_length(queue='priority') > 10:
                operation = self._grab_offer(offer, 'exl_prio')
                driver.acceptOffers([offer.id], [operation])
                continue
            if util.get_queue_length(queue='normal') > 10:
                operation = self._grab_offer(offer, None)
                driver.acceptOffers([offer.id], [operation])
                continue

            # otherwise let's decline.
            driver.declineOffer(offer.id)

    def _grab_offer(self, offer, resource_tag):
        """
        Grabs the offer from mesos and fires tasks.
        """
        offer_cpus = 0
        offer_mem = 0

        agent_ip = offer.url.address.ip
        agent_hostname = offer.hostname

        for resource in offer.resources:
            if resource.name == "cpus":
                offer_cpus += resource.scalar.value
            elif resource.name == "mem":
                offer_mem += resource.scalar.value

        # XXX: we take the complete offer here for now :-P
        # TODO: deal with offers with have 0 cpu in it...
        tid = uuid.uuid4()
        task = mesos_pb2.TaskInfo()
        task.task_id.value = str(tid)
        task.slave_id.value = offer.slave_id.value
        task.name = "OpenLava task %d" % tid
        task.executor.MergeFrom(self.executor)
        # this is the master host
        task.data = json.dumps({'master_hostname': self.master_host,
                                'master_ip': self.master_ip,
                                'agent_hostname': str(agent_hostname),
                                'agent_ip': str(agent_ip)})

        cpus = task.resources.add()
        cpus.name = "cpus"
        cpus.type = mesos_pb2.Value.SCALAR
        cpus.scalar.value = offer_cpus

        mem = task.resources.add()
        mem.name = "mem"
        mem.type = mesos_pb2.Value.SCALAR
        mem.scalar.value = offer_mem

        operation = mesos_pb2.Offer.Operation()
        operation.type = mesos_pb2.Offer.Operation.LAUNCH
        operation.launch.task_infos.extend([task])

        self.accepted_tasks[agent_hostname] = (offer_cpus, resource_tag)

        return operation

    def statusUpdate(self, driver, update):
        """
        Called to tell us about the status of our task by Mesos.
        """
        tmp = update.data.split(':')
        if len(tmp) < 2:
            return
        host = tmp[0]
        ip_addr = tmp[1]

        if host not in self.running_tasks:
            util.add_to_hosts(host, ip_addr)
            util.add_to_cluster_conf(host)
            # We tell the master to only expose those shares it should
            # expose and not more - currently JOB_SLOTS = CPU offers.
            tmp = self.accepted_tasks[host]
            max_jobs = tmp[0]
            resource_tag = tmp[1]
            util.add_host_to_cluster(host.strip(), max_jobs, resource_tag)
            self.running_tasks[host] = ip_addr
        elif update.state == mesos_pb2.TASK_FINISHED:
            util.rm_host_from_cluster(host.strip())
            util.rm_from_cluster_conf(host)
            util.rm_from_hosts(host)
            self.accepted_tasks.pop(host)
            self.running_tasks.pop(host)
        elif update.state == mesos_pb2.TASK_LOST \
                or update.state == mesos_pb2.TASK_KILLED \
                or update.state == mesos_pb2.TASK_FAILED:
            driver.abort()
            self.accepted_tasks.pop(host)
            self.running_tasks.pop(host)

        # TODO: use proper logging!
        print('Current queue length (normal): {0}'.
              format(util.get_queue_length('normal')))
        print('Current queue length (priority): {0}'.
              format(util.get_queue_length('priority')))
        print('Current number of hosts: {0}'.
              format(str(len(util.get_hosts()) - 2)))

        sys.stdout.flush()


if __name__ == '__main__':
    LOG.setLevel(level='DEBUG')

    EXECUTOR = mesos_pb2.ExecutorInfo()
    EXECUTOR.executor_id.value = "default"
    EXECUTOR.command.value = os.path.abspath("/tmp/openlava_node.sh")
    EXECUTOR.name = "OpenLava executor"
    EXECUTOR.source = "openlava_test"

    FRAMEWORK = mesos_pb2.FrameworkInfo()
    FRAMEWORK.user = ''
    FRAMEWORK.name = 'OpenLava'
    FRAMEWORK.webui_url = 'http://%s:9876' % ui.web.get_hostname()

    # Setup the loggers
    LOGGERS = (__name__, 'mesos')
    for log in LOGGERS:
        logging.getLogger(log).setLevel(logging.DEBUG)

    # TODO: authentication
    # TODO: revocable
    # TODO: pick up mesos master URI from env var.
    FRAMEWORK.principal = 'openlava-framework'
    DRIVER = native.MesosSchedulerDriver(OpenLavaScheduler(EXECUTOR),
                                         FRAMEWORK,
                                         'master:5050')
    STATUS = 0 if DRIVER.run() == mesos_pb2.DRIVER_STOPPED else 1

    DRIVER.stop()

    sys.exit(STATUS)
