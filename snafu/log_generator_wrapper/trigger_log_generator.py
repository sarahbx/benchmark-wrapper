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

import sys
import datetime
import time
from time import sleep
import random
import string
import logging
import boto3
import json
import subprocess

logger = logging.getLogger("snafu")


class Trigger_log_generator():
    def __init__(self, args):
        self.uuid = args.uuid
        self.cluster_name = args.cluster_name
        self.user = args.user
        self.size = args.size
        self.messages_per_minute = args.messages_per_minute
        self.messages_per_second = args.messages_per_second
        self.duration = args.duration
        self.pod_count = args.pod_count
        self.pod_name = args.pod_name
        self.timeout = args.timeout
        self.cloudwatch_log_group = args.cloudwatch_log_group
        self.aws_access_key = args.aws_access_key
        self.aws_secret_key = args.aws_secret_key
        self.aws_region = args.aws_region
        self.es_url = args.es_url
        self.es_token = args.es_token
        self.es_index = args.es_index

        if self.messages_per_minute:
            self.total_messages = self.messages_per_minute * self.duration
            self.messages_per_second = self.messages_per_minute / 60
        elif args.messages_per_second:
            self.total_messages = self.messages_per_second * 60 * self.duration
            self.messages_per_second = self.messages_per_second
        else:
            print("NO RATE DEFINED EXITING")
            exit(1)
        self.delay = 1 / self.messages_per_second
        self.my_message = ''.join(random.choice(string.ascii_uppercase + string.digits)
                                  for x in range(self.size))

    def _json_payload(self, data):
        payload = {
            "uuid": self.uuid,
            "cluster_name": self.cluster_name,
            "pod_name": self.pod_name,
            "expected_duration": self.duration * 60,
            "total_expected_messages": self.total_messages,
            "messages_per_second": self.messages_per_second,
            "messages_per_minute": self.messages_per_minute,
            "message_size": self.size,
            "timeout": self.timeout,
            "pod_count": self.pod_count,
            "user": self.user
        }
        if self.cloudwatch_log_group:
            backend = {
                "cloudwatch_log_group": self.cloudwatch_log_group,
                "backend": "cloudwatch"
            }
            payload.update(backend)
        elif self.es_url:
            backend = {
                "es_url": self.es_url,
                "es_index": self.es_index,
                "backend": "elasticsearch"
            }
            payload.update(backend)
        payload.update(data)
        return payload

    def _run_log_test(self):
        # Custom logging settings for log tests
        gen_logger = logging.getLogger("logGen")
        gen_logger.setLevel(logging.DEBUG)
        handler = logging.StreamHandler(sys.stdout)
        handler.setLevel(logging.DEBUG)
        formatter = logging.Formatter('%(message)s')
        handler.setFormatter(formatter)
        gen_logger.addHandler(handler)
        count = 0
        while count < self.total_messages:
            if self.messages_per_minute:
                gen_logger.info(self.my_message)
                sleep(self.delay)
                count += 1
            else:
                t = datetime.datetime.now()
                for x in range(0, self.messages_per_second):
                    gen_logger.info(self.my_message)
                tdiff = datetime.datetime.now() - t
                total_diff = tdiff.seconds + (tdiff.microseconds / 1000000)
                if total_diff < 1:
                    sleep(1 - total_diff)
                count += self.messages_per_second
        return count

    def _check_cloudwatch(self,start_time,end_time):
        logger.info("Checking CloudWatch for expected messages")
        if self.aws_access_key is not None and self.aws_secret_key is not None:
            client = boto3.client(service_name='logs',
                                  region_name=self.aws_region,
                                  aws_access_key_id=self.aws_access_key,
                                  aws_secret_access_key=self.aws_secret_key)
        else:
            client = boto3.client(service_name='logs', region_name=self.aws_region)

        query = "fields @timestamp, @message | filter message = \""\
                + self.my_message + "\" | stats count()"

        start_query_response = client.start_query(
            logGroupName=self.cloudwatch_log_group,
            startTime=start_time - 60,
            endTime=end_time + 60,
            queryString=query
        )

        running = True
        while running:
            query_status = client.describe_queries(
                logGroupName=self.cloudwatch_log_group
            )
            for x in range(0,len(query_status['queries'])):
                if query_status['queries'][x]['queryId'] == start_query_response.get('queryId'):
                    if query_status['queries'][x]['status'] == "Complete":
                        running = False
            sleep(1)

        query_results = client.get_query_results(queryId=start_query_response.get('queryId'))
        return int(query_results['statistics']['recordsMatched'])

    def _check_es(self,start_time,end_time):
        logger.info("Checking ElasticSearch for expected messages")
        header_json = 'Content-Type: application/json'
        if self.es_token:
            header_auth = "Authorization: Bearer " + self.es_token
        s_time = datetime.datetime.fromtimestamp(start_time - 60).strftime("%Y-%m-%dT%H:%M:%S")
        e_time = datetime.datetime.fromtimestamp(end_time + 60).strftime("%Y-%m-%dT%H:%M:%S")
        data = {
            "query": {
                "bool": {
                    "must": [
                        {
                            "match": {
                                "message": self.my_message
                            }
                        }
                    ],
                    "filter": [
                        {
                            "range": {
                                "@timestamp": {
                                    "gte": s_time,
                                    "lte": e_time
                                }
                            }
                        }
                    ]
                }
            }
        }

        es_url = self.es_url + "/" + self.es_index + "/_count"

        try:
            if self.es_token:
                response = subprocess.check_output(['curl','--insecure','--header',header_auth,
                                                    '--header',header_json,es_url,'-d',json.dumps(data),
                                                    '-s']).decode("utf-8")
            else:
                response = subprocess.check_output(['curl','--insecure','--header',
                                                    header_json,es_url,'-d',json.dumps(data),
                                                    '-s']).decode("utf-8")
        except Exception as err:
            logging.info("ElasticSearch query failed")
            logging.info(err)
            return 0
        try:
            return(json.loads(response)['count'])
        except Exception as err:
            logging.info("No valid json returned")
            logging.info(err)
            return 0

    def emit_actions(self):
        logger.info("Running log test with %d byte size for %d minutes at a rate of %d messages per second" %
                    (self.size, self.duration, self.messages_per_second))
        logger.info("Test UUID is %s on cluster %s" % (self.uuid, self.cluster_name))

        timestamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        start_time = time.time()
        message_count = self._run_log_test()
        end_time = time.time()
        elapsed_time = end_time - start_time

        logger.info("All messages sent")

        if self.cloudwatch_log_group or self.es_url:
            logger.info("Confirming all %d messages received in backend" % (message_count))
            received_all_messages = False
            current_time = time.time()
            while not received_all_messages and current_time <= end_time + self.timeout:
                if self.cloudwatch_log_group:
                    messages_received = self._check_cloudwatch(int(start_time),int(end_time))
                else:
                    messages_received = self._check_es(int(start_time),int(end_time))
                if messages_received == message_count:
                    received_all_messages = True
                else:
                    logger.info("Message check failed. Retrying until timeout")
                    sleep(1)
                    current_time = time.time()
                post_complete_time = time.time() - end_time
                message_confirmed_received = {"messages_confirmed_received": received_all_messages,
                                              "messages_received": messages_received,
                                              "post_complete_time": int(post_complete_time)}
            if not received_all_messages:
                logger.info("Not all messages received by backend.")
                logger.info("Total messages received: %d Total messages expected: %d" %
                            (messages_received, message_count))
                logger.info("Seconds backend waited for messages: %d" %
                            (int(post_complete_time)))
            else:
                logger.info("All messages received by backend")
                logger.info("Total messages received: %d Total messages expected: %d" %
                            (messages_received, message_count))
                logger.info("Seconds till backend received all messages: %d" %
                            (int(post_complete_time)))

        data = {"timestamp": timestamp,
                "actual_duration": int(elapsed_time),
                "message_count": message_count}

        if self.cloudwatch_log_group or self.es_url:
            data.update(message_confirmed_received)

        es_data = self._json_payload(data)
        yield es_data, 'results'
        logger.info("Finished executing logging test")
