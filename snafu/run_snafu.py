#!/usr/bin/env python
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.

# This wrapper assumes the following in fiojob
# per_job_logs=true
#
import os
import sys
import argparse
import elasticsearch
import time
import datetime
import logging
import hashlib
import urllib3
import json
import ssl
from distutils.util import strtobool
from snafu.utils.common_logging import setup_loggers
from snafu.utils.get_prometheus_data import get_prometheus_data
from snafu.utils.wrapper_factory import wrapper_factory
from snafu.utils.request_cache_drop import drop_cache
from snafu.utils.py_es_bulk import streaming_bulk

logger = logging.getLogger("snafu")

# mute elasticsearch and urllib3 logging
es_log = logging.getLogger("elasticsearch")
es_log.setLevel(logging.CRITICAL)
urllib3_log = logging.getLogger("urllib3")
urllib3_log.setLevel(logging.CRITICAL)


def main():
    # collect arguments
    parser = argparse.ArgumentParser(description="run script", add_help=False)
    parser.add_argument(
        '-v', '--verbose', action='store_const', dest='loglevel', const=logging.DEBUG,
        default=logging.INFO, help='enables verbose wrapper debugging info')
    parser.add_argument(
        '-t', '--tool', help='Provide tool name', required=True)
    parser.add_argument(
        '--run-id', help='Run ID to unify benchmark results in ES',
        nargs='?', default="NA"
    )
    index_args, unknown = parser.parse_known_args()
    index_args.index_results = False
    index_args.prefix = "snafu-%s" % index_args.tool

    setup_loggers("snafu", index_args.loglevel)
    log_level_str = 'DEBUG' if index_args.loglevel == logging.DEBUG else 'INFO'
    logger.info("logging level is %s" % log_level_str)

    # set up a standard format for time
    FMT = '%Y-%m-%dT%H:%M:%SGMT'

    # instantiate elasticsearch instance and check connection
    es_settings = {}
    es_settings["server"] = os.getenv("es")
    es_settings["verify_cert"] = os.getenv("es_verify_cert", "true").lower()
    if es_settings["server"] and ":443" in es_settings["server"]:
        es_settings["verify_cert"] = "false"
    if es_settings["server"]:
        index_args.prefix = os.getenv("es_index", "")
        logger.info("Using elasticsearch server with host: %s" % es_settings['server'])
        logger.info("Using index prefix for ES: %s" % index_args.prefix)
        index_args.index_results = True
        try:
            if es_settings["verify_cert"] == "false":
                logger.info("Turning off TLS certificate verification")
                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
                ssl_ctx = ssl.create_default_context()
                ssl_ctx.check_hostname = False
                ssl_ctx.verify_mode = ssl.CERT_NONE
                es = elasticsearch.Elasticsearch([es_settings["server"]], send_get_body_as='POST',
                                                 ssl_context=ssl_ctx, use_ssl=True)
            else:
                es = elasticsearch.Elasticsearch([es_settings["server"]], send_get_body_as='POST')
            logger.info("Connected to the elasticsearch cluster with info as follows:")
            logger.info(json.dumps(es.info(), indent=4))
        except Exception as e:
            logger.warn("Elasticsearch connection caused an exception: %s" % e)
            index_args.index_results = False

    index_args.document_size_capacity_bytes = 0
    # call py es bulk using a process generator to feed it ES documents
    if index_args.index_results:
        parallel_setting = strtobool(os.environ.get('parallel', "false"))
        res_beg, res_end, res_suc, res_dup, res_fail, res_retry = streaming_bulk(es,
                                                                                 process_generator(
                                                                                     index_args,
                                                                                     parser),
                                                                                 parallel_setting)

        logger.info(
            "Indexed results - %s success, %s duplicates, %s failures, with %s retries." % (
                res_suc,
                res_dup,
                res_fail,
                res_retry))

        start_t = time.strftime('%Y-%m-%dT%H:%M:%SGMT', time.gmtime(res_beg))
        end_t = time.strftime('%Y-%m-%dT%H:%M:%SGMT', time.gmtime(res_end))

    else:
        start_t = time.strftime('%Y-%m-%dT%H:%M:%SGMT', time.gmtime())
        # need to loop through generator and pass on all yields
        # this will execute all jobs without elasticsearch
        for i in process_generator(index_args, parser):
            pass
        end_t = time.strftime('%Y-%m-%dT%H:%M:%SGMT', time.gmtime())

    start_t = datetime.datetime.strptime(start_t, FMT)
    end_t = datetime.datetime.strptime(end_t, FMT)

    # get time delta for indexing run
    tdelta = end_t - start_t
    total_capacity_bytes = index_args.document_size_capacity_bytes
    logger.info("Duration of execution - %s, with total size of %s bytes" % (tdelta, total_capacity_bytes))

def process_generator(index_args, parser):
    benchmark_wrapper_object_generator = generate_wrapper_object(index_args, parser)

    for wrapper_object in benchmark_wrapper_object_generator:
        for data_object in wrapper_object.run():
            # drop cache after every sample
            drop_cache()
            for action, index in data_object.emit_actions():
                if "get_prometheus_trigger" in index and "prom_es" in os.environ:
                    # Action will contain the following
                    """
                    action: {
                              "uuid": <uuid>
                              "user": <user>
                              "clustername": <clustername>
                              "sample": <int>
                              "starttime": <datetime> datetime.utcnow().strftime('%s')
                              "endtime": <datetime>
                              test_config: {...}
                            }
                    """

                    index_prom_data(index_args, action)
                else:
                    es_valid_document = get_valid_es_document(action,
                                                              index,
                                                              index_args)
                    yield es_valid_document

def generate_wrapper_object(index_args, parser):
    benchmark_wrapper_object = wrapper_factory(index_args.tool, parser)

    yield benchmark_wrapper_object

def get_valid_es_document(action, index, index_args):
    if index != '':
        es_index = index_args.prefix + '-' + index
    else:
        es_index = index_args.prefix
    es_valid_document = {"_index": es_index,
                         "_op_type": "create",
                         "_source": action,
                         "_id": ""}
    logger.debug("Run ID is {index_args.run_id}")
    es_valid_document['run_id'] = action['run_id'] = index_args.run_id
    es_valid_document["_id"] = hashlib.sha256(str(action).encode()).hexdigest()
    document_size_bytes = sys.getsizeof(es_valid_document)
    index_args.document_size_capacity_bytes += document_size_bytes
    logger.debug("document size is: %s" % document_size_bytes)
    logger.debug(json.dumps(es_valid_document, indent=4, default=str))

    return es_valid_document

def index_prom_data(index_args, action):
    es_settings = {}

    # definition of prometheus data getter, will yield back prom doc
    def get_prometheus_generator(index_args, action):
        prometheus_doc_generator = get_prometheus_data(action)
        for prometheus_doc in prometheus_doc_generator.get_all_metrics():
            es_valid_document = get_valid_es_document(prometheus_doc, "prometheus_data", index_args)
            yield es_valid_document

    es_settings["server"] = os.getenv("prom_es")
    es_settings["verify_cert"] = os.getenv("es_verify_cert", "true")
    if ":443" in es_settings["server"] :
        es_settings["verify_cert"] = "false"
    if es_settings["server"]:
        index_args.prefix = os.getenv("es_index", "")
        logger.info("Using Prometheus elasticsearch server with host: %s" % es_settings["server"])
        logger.info("Using index prefix for prometheus ES: %s" % index_args.prefix)
        index_args.index_results = True
        try:
            if es_settings["verify_cert"] == "false":
                logger.info("Turning off TLS certificate verification for Prometheus ES indexer")
                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
                ssl_ctx = ssl.create_default_context()
                ssl_ctx.check_hostname = False
                ssl_ctx.verify_mode = ssl.CERT_NONE
                es = elasticsearch.Elasticsearch([es_settings["server"]], send_get_body_as='POST',
                                                 ssl_context=ssl_ctx, use_ssl=True)
            else:
                es = elasticsearch.Elasticsearch([es_settings["server"]], send_get_body_as='POST')
            logger.info("Connected to the elasticsearch cluster with info as follows:")
            logger.info(json.dumps(es.info(), indent=4))
        except Exception as e:
            logger.warn("Elasticsearch connection caused an exception: %s" % e)
            index_args.index_results = False

    # check that we want to index and that the prom_es exist.
    if index_args.index_results:
        logger.info("initializing prometheus indexing")
        parallel_setting = strtobool(os.environ.get('parallel', "false"))
        res_beg, res_end, res_suc, res_dup, res_fail, res_retry = streaming_bulk(es,
                                                                                 get_prometheus_generator(
                                                                                     index_args,
                                                                                     action),
                                                                                 parallel_setting)

        logger.info(
            "Prometheus indexed results - %s success, %s duplicates, %s failures, with %s retries." % (
                res_suc,
                res_dup,
                res_fail,
                res_retry))
        start_t = time.strftime('%Y-%m-%dT%H:%M:%SGMT', time.gmtime(res_beg))
        end_t = time.strftime('%Y-%m-%dT%H:%M:%SGMT', time.gmtime(res_end))
        # set up a standard format for time
        FMT = '%Y-%m-%dT%H:%M:%SGMT'
        start_t = datetime.datetime.strptime(start_t, FMT)
        end_t = datetime.datetime.strptime(end_t, FMT)

        # get time delta for indexing run
        tdelta = end_t - start_t
        logger.info("Prometheus indexing duration of execution - %s" % tdelta)


if __name__ == "__main__":
    sys.exit(main())
