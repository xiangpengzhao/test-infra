# Copyright 2017 The Kubernetes Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Receive push events for new builds and upload rows to BigQuery.
"""

from __future__ import print_function

import argparse
import json
import os
import pprint
import sys
import traceback
import time

import multiprocessing.pool

try:
    from google.cloud import bigquery
    from google.cloud import pubsub
    import google.cloud.exceptions
except ImportError, e:
    print('WARNING: unable to load google cloud (test environment?)')
    traceback.print_exc()

import model
import make_db
import make_json


def process_changes(results):
    """Given a list of GCS change events, return messages to ack and
    jobs/builds to further process.
    """
    acks = []  # pubsub message ids to acknowledge
    todo = []  # (id, job, build) of builds to grab

    # process results, find finished builds to process
    for ack_id, message in results:
        if message.attributes['event_type'] != 'OBJECT_FINALIZE':
            acks.append(ack_id)
            continue
        obj = message.attributes['object_id']
        if not obj.endswith('/finished.json'):
            acks.append(ack_id)
            continue
        job, build = obj[:-len('/finished.json')].rsplit('/', 1)
        job = 'gs://%s/%s' % (message.attributes['bucket_id'], job)
        todo.append((ack_id, job, build))

    return acks, todo


def get_started_finished(gcs_client, db, todo):
    """Download started/finished.json from build dirs in todo."""
    acks = []
    build_dirs = []
    pool = multiprocessing.pool.ThreadPool(16)
    try:
        for ack_id, (build_dir, started, finished) in pool.imap_unordered(
                lambda (ack_id, job, build): (ack_id, gcs_client.get_started_finished(job, build)),
                todo):
            # build_dir, started, finished = gcs_client.get_started_finished(job, build)
            if finished:
                if not db.insert_build(build_dir, started, finished):
                    print('already present??')
                print(build_dir, bool(started), bool(finished),
                      started and time.strftime('%F %T %Z', time.localtime(started.get('timestamp', 0))),
                      finished and finished.get('result'))
                build_dirs.append(build_dir)
                acks.append(ack_id)
            else:
                print('???', build_dir, started, finished)
    finally:
        pool.close()
    db.commit()
    return acks, build_dirs


def row_to_mapping(row, schema):
    """Convert a dictionary to a list for bigquery.Table.insert_data.

    Silly. See https://github.com/GoogleCloudPlatform/google-cloud-python/issues/3396
    """
    return [row.get(field.name, [] if field.mode == 'REPEATED' else None) for field in schema]


def insert_data(table, rows_iter):
    """Upload rows from rows_iter into bigquery table table.

    rows_iter should return a series of (row_id, row dictionary) tuples.
    The row dictionary must match the table's schema.

    Returns the row_ids that were inserted.
    """
    emitted = set()

    rows = []
    row_ids = []

    for row_id, row in rows_iter:
        emitted.add(row_id)
        if len(json.dumps(row)) > 1e6:
            print('ERROR: row too long', row['path'])
            continue
        row = row_to_mapping(row, table.schema)
        rows.append(row)
        row_ids.append(row_id)

    def insert(table, rows, row_ids):
        while True:
            try:
                errors = table.insert_data(rows, row_ids, skip_invalid_rows=True)
                break
            except google.cloud.exceptions.ServerError:
                # retry
                traceback.print_exc()
                time.sleep(5)

        if not errors:
            print('Loaded {} builds into {}'.format(len(rows), table.name))
        else:
            print('Errors:')
            pprint.pprint(errors)
            pprint.pprint(table.schema)

    if len(json.dumps(rows)) > 10e6:
        print('WARNING: too big for one insert, doing stupid slow version')
        for row, row_id in zip(rows, row_ids):
            insert(table, [row], [row_id])
    else:
        insert(table, rows, row_ids)

    return emitted


def main(db, sub, tables, client_class=make_db.GCSClient, stop=None):
    gcs_client = client_class('', {})

    if stop is None:
        stop = lambda: False

    results = [0] * 1000  # don't sleep on first loop
    while not stop():
        print()
        if len(results) < 10 and client_class is make_db.GCSClient:
            time.sleep(5)  # slow down!
        print('====', time.strftime("%F %T %Z"), '=' * 40)

        results = sub.pull(max_messages=5000)
        start = time.time()
        while time.time() < start + 7:
            results_more = sub.pull(max_messages=1000, return_immediately=True)
            if not results_more:
                break
            results += results_more

        print('PULLED', len(results))

        acks, todo = process_changes(results)

        if acks:
            print('ACK irrelevant', len(acks))
            for x in xrange(0, len(acks), 1000):
                sub.acknowledge(acks[x: x + 1000])

        if todo:
            print('EXTEND-ACK ', (len(todo)))
            # give 3 minutes to grab build details
            sub.modify_ack_deadline([i for i, j, b in todo], 60*3)

        acks, build_dirs = get_started_finished(gcs_client, db, todo)

        # notify pubsub queue that we've handled the finished.json messages
        if acks:
            print('ACK "finished.json"', len(acks))
            sub.acknowledge(acks)

        # grab junit files for new builds
        make_db.download_junit(db, 16, client_class)

        # stream new rows to tables
        if build_dirs and tables:
            for table, incremental_table in tables.itervalues():
                builds = db.get_builds_from_paths(build_dirs, incremental_table)
                emitted = insert_data(table, make_json.make_rows(db, builds))
                db.insert_emitted(emitted, incremental_table)


def get_options(argv):
    """Process command line arguments."""
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--poll',
        required=True,
        help='Follow GCS changes from project/topic/subscription',
    )
    parser.add_argument(
        '--dataset',
        help='BigQuery dataset (e.g. k8s-gubernator:build)'
    )
    parser.add_argument(
        '--tables',
        nargs='+',
        default=[],
        help='Upload rows to table:days [e.g. --tables day:1 week:7 all:0]',
    )
    parser.add_argument(
        '--stop_at',
        type=int,
        help='Terminate when this hour (0-23) rolls around (in local time).'
    )
    return parser.parse_args(argv)


def load_sub(poll):
    """Return the PubSub subscription specificed by the /-separated input."""
    project, topic, subscription = OPTIONS.poll.split('/')
    pubsub_client = pubsub.Client(project)
    return pubsub_client.topic(topic).subscription(subscription)


def load_schema(schemafield):
    """Construct the expected BigQuery schema from files on disk.

    Only used for new tables."""
    basedir = os.path.dirname(__file__)
    schema_json = json.load(open(os.path.join(basedir, 'schema.json')))
    def make_field(spec):
        spec['field_type'] = spec.pop('type')
        if 'fields' in spec:
            spec['fields'] = [make_field(f) for f in spec['fields']]
        return schemafield(**spec)
    return [make_field(s) for s in schema_json]


def load_tables(dataset, tablespecs):
    """Construct a dictionary of BigQuery tables given the input tablespec.

    Returns {name: (bigquery.Table, incremental table name)}
    """
    project, dataset_name = OPTIONS.dataset.split(':')
    dataset = bigquery.Client(project).dataset(dataset_name)

    tables = {}
    for spec in tablespecs:
        name, days = spec.split(':')
        table = dataset.table(name)
        try:
            table.reload()
        except google.cloud.exceptions.NotFound:
            table.schema = load_schema(bigquery.schema.SchemaField)
            table.create()
        tables[name] = (table, make_json.get_table(float(days)))
    return tables


class StopWhen(object):
    """A simple object that returns True once when the given hour begins."""
    def __init__(self, target, clock=lambda: time.localtime().tm_hour):
        self.clock = clock
        self.last = self.clock()
        self.target = target

    def __call__(self):
        now = self.clock()
        last = self.last
        self.last = now
        return now != last and now == self.target


if __name__ == '__main__':
    OPTIONS = get_options(sys.argv[1:])

    stop = None
    if OPTIONS.stop_at:
        stop = StopWhen(OPTIONS.stop_at)

    main(model.Database('build.db'),
         load_sub(OPTIONS.poll),
         load_tables(OPTIONS.dataset, OPTIONS.tables),
         stop=stop)
