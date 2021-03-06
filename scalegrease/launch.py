import dns.resolver
import logging
import re
import os
import glob
import abc
import json
import shutil
import socket
import xml.etree.ElementTree

import kazoo.client
import samsa.cluster
import time
import xml.etree

from scalegrease import error
from scalegrease import system
from scalegrease import common


def maven_output(mvn_cmd):
    output = system.run_with_logging(mvn_cmd)
    logging.debug(output)
    return output


def launch(crontab_glob, pom_file, offline, conf):
    offline_flag = ["--offline"] if offline else []
    all_crontabs = glob.glob(crontab_glob)
    if not all_crontabs:
        logging.warn("No crontab files found matching '%s', pwd=%s.  Existing production crontabs "
                     "will be deleted", crontab_glob, os.getcwd())
    # Now, it would be great if maven could spit out structured output.  This is fragile.
    effective_pom_output = maven_output(["mvn"] + offline_flag + ["--file", pom_file,
                                                                  "help:effective-pom"])
    def tag_value(elem, tag):
        return filter(lambda e: re.match(r"\{.*\}" + tag, e.tag), list(elem))[0].text

    pom_text = re.search(r"<project .*</project>", effective_pom_output, re.DOTALL).group(0)
    pom = xml.etree.ElementTree.XML(pom_text)
    group_id = tag_value(pom, "groupId")
    artifact_id = tag_value(pom, "artifactId")

    logging.info("Determined groupId and artifactId: %s:%s", group_id, artifact_id)

    launch_conf = conf['launch']
    launcher_class = system.load_class(launch_conf["launcher_class"])
    launcher = launcher_class(launch_conf)
    launcher.launch(group_id, artifact_id, all_crontabs)


class Launcher(object):
    __metaclass__ = abc.ABCMeta

    def __init__(self, config):
        self._config = config

    @abc.abstractmethod
    def launch(self, group_id, artifact_id, crontabs):
        raise NotImplementedError()

    @abc.abstractmethod
    def snatch(self):
        raise NotImplementedError()


class KafkaLauncher(Launcher):
    VERSION = 1

    def _find_zk_hosts(self):
        zk_conf = self._config['zookeeper']
        hosts_conf = zk_conf.get('hosts')
        srv_conf = zk_conf.get('srv')
        if hosts_conf:
            if srv_conf:
                logging.warn("Found both hosts and srv zookeeper configuration, using hosts")
            return hosts_conf
        answers = dns.resolver.query(srv_conf, 'SRV')
        return ','.join(["{0}:{1}".format(a.target.to_text(), a.port) for a in answers])

    def _obtain_topic(self):
        topic_name = str(self._config['kafka_launcher_topic'])
        zk_hosts = self._find_zk_hosts()
        zk = kazoo.client.KazooClient(hosts=zk_hosts)
        zk.start()
        cluster = samsa.cluster.Cluster(zk)
        topic = cluster.topics[topic_name]
        return topic, zk

    def _validate_name(self, name):
        if not re.match(r"[a-zA-Z0-9\._,:@-]{1,100}", name):
            raise ValueError(
                'Invalid component name, please use letters and digits: "{0}"'.format(name))

    def launch(self, group_id, artifact_id, crontabs):
        self._validate_name(group_id)
        self._validate_name(artifact_id)
        crontab_contents = []
        for cr in crontabs:
            self._validate_name(cr)
            crontab_contents.append({'name': os.path.basename(cr), 'content': system.read_file(cr) + '\n'})
        msg = {'version': self.VERSION, 'group_id': group_id, 'artifact_id': artifact_id,
               'crontabs': crontab_contents}
        json_msg = json.dumps(msg)
        topic, zk = self._obtain_topic()
        topic.publish(json_msg)
        # TODO: Use context manager, aka with statement
        zk.stop()

    def snatch(self):
        topic, zk = self._obtain_topic()
        group_name = b"scalegrease-snatch-consumer-{0}-{1}".format(
            socket.gethostname(),  int(time.time()))
        consumer = topic.subscribe(group_name)
        while True:
            msg = consumer.next_message(block=True, timeout=1)
            if not msg:
                break
            try:
                self._handle_cron_msg(msg)
            except:
                logging.exception('Failed to handle message, proceeding to next:\n  "%s"', msg)

        # TODO: Use context manager, aka with statement
        zk.stop()

    def _handle_cron_msg(self, msg):
        logging.info('Processing launch message: "%s"', msg)
        json_msg = json.loads(msg)
        if json_msg['version'] != self.VERSION:
            logging.info("Retrieved wrong message version, expected %d, discarding: %s",
                         self.VERSION, msg)
            return
        group_id = json_msg['group_id']
        artifact_id = json_msg['artifact_id']
        crontabs = json_msg['crontabs']
        self._validate_name(group_id)
        self._validate_name(artifact_id)
        crontab_name_contents = dict([(ct['name'], ct['content']) for ct in crontabs])
        for crontab_name in crontab_name_contents:
            self._validate_name(crontab_name)
        self._update_crontabs(group_id, artifact_id, crontab_name_contents)

    def _package_prefix(self, group_id, artifact_id):
        return "{0}__{1}__{2}__".format(self._config["crontab_unique_prefix"], group_id,
                                        artifact_id)

    def _update_crontabs(self, group_id, artifact_id, crontab_name_contents):
        cron_dst_dir = self._config["crontab_etc_crond"]
        package_prefix = self._package_prefix(group_id, artifact_id)
        existing_crontabs = filter(lambda ct: ct.startswith(package_prefix),
                                   os.listdir(cron_dst_dir))
        # Crontab files must not contain a dot, or they will not be run.
        def name_transform(ct):
            return "{0}/{1}".format(os.path.dirname(ct), os.path.basename(ct).replace(".", "_"))
        for existing in existing_crontabs:
            if existing[len(package_prefix):] not in map(name_transform, crontab_name_contents):
                stale_path = "{0}/{1}".format(cron_dst_dir, existing)
                logging.info("rm %s", stale_path)
                try:
                    os.remove(stale_path)
                except IOError:
                    logging.exception("Failed to remove %s", stale_path)
        for tab_name, tab_contents in crontab_name_contents.items():
            dst_path = name_transform("{0}/{1}{2}".format(cron_dst_dir, package_prefix, tab_name))
            if not os.path.isfile(dst_path) or tab_contents != system.read_file(dst_path):
                logging.info("Writing crontab %s, contents:\n  %s", dst_path, tab_contents)
                try:
                    system.write_file(dst_path, tab_contents)
                except IOError:
                    logging.exception("Failed to install crontab %s", dst_path)
            else:
                os.utime(dst_path, None)


def extra_arguments_adder(parser):
    parser.add_argument("--cron-glob", "-g", default="src/main/cron/*.cron",
                        help="Glob pattern for enumerating cron files")
    parser.add_argument("--mvn-offline", "-o", action="store_true",
                        help="Use Maven in offline mode")
    parser.add_argument("--pom-file", "-p", default="pom.xml",
                        help="Path to project pom file")


def log_path_infix(args):
    # The arguments are not so interesting to qualify the path with
    return "launcher/"


def main(argv):
    args, conf = common.initialise(argv, extra_arguments_adder, log_path_infix)

    try:
        launch(args.cron_glob, args.pom_file, args.mvn_offline, conf)
    except error.Error:
        logging.exception("Job failed")
        return 1
