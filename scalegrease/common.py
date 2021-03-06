import argparse
import json
import logging
import os
import datetime
import socket
import getpass

from scalegrease import system


# This module is for code common to scalegrease that is not system-level enough
# to be put in the system module


# TODO(arash): We've added so many arguments here now, I think it would be
# better to use an abstract class where the passed functions would be
# implemented methods instead.
def initialise(argv, extra_arguments_adder, log_path_infix):
    """ Code common to all scalegrease executables """
    parser = argparse.ArgumentParser()

    parser.add_argument("--config-file", "-c", default="/etc/scalegrease.json",
                        help="Read configuration from CONFIG_FILE. "
                             "Environment variables in the content will be "
                             "expanded.")

    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Increase debug verbosity")

    parser.add_argument("--no-random-delay", "-D", action="store_true",
                        help="Force no random delay ever (default adds random 60s delay running from cron)")

    extra_arguments_adder(parser)
    args = parser.parse_args(argv[1:])
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)
    logging.info("Reading configuration from %s", args.config_file)
    config_file_contents = system.read_file(args.config_file)
    config_expanded = os.path.expandvars(config_file_contents)
    config = json.loads(config_expanded)
    logging.debug("Configuration read:\n%s", config)
    parse_config_common(config.get("common"), log_path_infix(args))
    return args, config


def parse_config_common(config, instantiated_log_path_infix):
    if config is None:
        logging.warn('No "common" value is configured, skipping ...')
    else:
        parse_config_common_file_logger(config.get("file_logger"),
                                        instantiated_log_path_infix)


def parse_config_common_file_logger(config, instantiated_log_path_infix):
    if config is None:
        logging.warn('No "file_logger" value is configured, skipping ...')
    else:
        # We add username so we're less likely to get permission issues
        path_prefix = "{config_stripped}/{username}/{short_hostname}/".format(
            config_stripped=config.rstrip("/"),
            username=getpass.getuser(),
            short_hostname=get_short_hostname(),
        )
        now = datetime.datetime.now()
        log_filename = '{0:%Y-%m-%d}.log'.format(now)
        logging_directory = path_prefix + instantiated_log_path_infix
        try:
            system.mkdir_p(logging_directory)
        except:
            logging.warn("Could not create directory for logging, skipping. "
                         "Directory: %s", logging_directory)
            return

        # We include the process id since we can imagine multiple instances
        # writing to the same file. Also, we do not need any log rotation,
        # since all the binaries will be short-lived (way less than a day).
        # Eventually we should maybe clean up the log files.
        FORMAT = ('[%(process)d] %(asctime)-s'
                  ' %(levelname)s:%(name)s %(message)s')
        total_path = logging_directory + log_filename
        file_handler = logging.FileHandler(total_path)
        logging.info("Adding file logger with path: %s", total_path)

        # We don't log the date because it's contained in the file name. Also
        # we skip milliseconds, since scalegrease might in itself call
        # scalegrease, increasing risk for log-blindness.
        formatter = logging.Formatter(fmt=FORMAT, datefmt='%H:%M:%S')
        file_handler.setFormatter(formatter)
        root_logger = logging.getLogger()
        root_logger.addHandler(file_handler)


def get_short_hostname():
    """ If hostname == 'lon4-abc.acme.net', return 'lon4-abc' """
    return socket.gethostname().split(".")[0]
