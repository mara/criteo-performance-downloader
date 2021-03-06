#!/usr/bin/python

import datetime
import errno
import gzip
import json
import logging
import shutil
import tempfile
import time
import xml.etree.ElementTree as etree
from collections import namedtuple, defaultdict
from datetime import datetime, timedelta
from os.path import abspath
from pathlib import Path
from urllib.request import urlopen
from xml.etree.ElementTree import ParseError
from xml.sax._exceptions import SAXParseException

from criteo_downloader import config
from criteo_downloader.config import CriteoAccount
from suds.sudsobject import asdict

OUTPUT_FILE_VERSION = 'v2'


def create_criteo_client(account: CriteoAccount):
    """Creates a criteo API client for a given Criteo account

    Args:
        account: A Criteo account

    Returns:
        A pycriteo API client

    """
    from pycriteo import Client
    return Client(username=account.username, password=account.password, token=account.token)


def download_data():
    """Creates the pycriteo API clients and downloads the data"""
    logger = logging.basicConfig(level=logging.INFO,
                                 format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

    accounts = config.accounts()
    for account in accounts:
        api_client = create_criteo_client(account)
        download_data_set(api_client, account)


def download_data_set(api_client, account: CriteoAccount):
    """Downloads the account structure and the Criteo campaign performance

    Args:
        api_client: A pycriteo API client
        account: A Criteo Account

    """
    for attempt_number in range(config.retry_attempts()):
        try:
            download_performance(api_client, account)
            break
        except (SAXParseException, ParseError) as e:
            if attempt_number == config.retry_attempts() - 1:
                logging.info(f'XML error trying to download account {account}: {e}, too many attempts. Giving up...')
                raise e
            logging.info(
                f'XML error trying to download account {account} on attempt {attempt_number}: {e}, retrying...')
            time.sleep(config.retry_timeout())
    download_account_structure(api_client, account)


def download_performance(api_client, account: CriteoAccount):
    """Downloads the performance data for a give Criteo account

        Args:
            api_client: A pycriteo API client
            account: A Criteo Account

        """
    job_ids = schedule_report_jobs(api_client)

    for job_id in job_ids:
        if not is_job_completed(api_client, job_id):
            logging.info('report {job_id} not ready yet'.format(job_id=job_id))

        logging.info('downloading performance report {job_id} for account {account}'.format(
            job_id=job_id,
            account=account))
        download_url = api_client.getReportDownloadUrl(job_id)
        table = etree.parse(urlopen(download_url)).getroot().getchildren()[0]
        rows = [i for i in table if i.tag == 'rows'][0]
        report_data = defaultdict()  ##to easily append later on
        for row in rows:  ## create dictionary with days as key, each day contains a list of campaign performances
            if row.attrib['dateTime'] not in report_data:
                report_data[row.attrib['dateTime']] = []
            report_data[row.attrib['dateTime']].append(row.attrib.copy())
        for day in report_data:  ## write out in json format
            relative_filepath = Path(
                '{date}/criteo/campaign-performance-{account_filename}-{version}.json.gz'.format(
                    date=day.replace('-', '/'),
                    account_filename=account.normalized_name,
                    version=OUTPUT_FILE_VERSION))

            day_datetime = datetime.strptime(day, '%Y-%m-%d')
            last_datetime = datetime.now() - timedelta(days=1)

            # download if not existed or (re-)download if day in the redownload_window timeframe
            if (not ensure_data_directory(relative_filepath).is_file()
                    or (last_datetime - day_datetime).days <= int(config.redownload_window())):
                logging.info('download {} campaign-performance for day: {}'.format(account.account_name, day))
                filepath = abspath(ensure_data_directory(relative_filepath))
                with tempfile.TemporaryDirectory() as tmp_dir:
                    tmp_filepath = Path(tmp_dir, filepath)
                    with gzip.open(str(filepath), 'wt') as criteo_performance_file:
                        criteo_performance_file.write(json.dumps(report_data[day]))

                    shutil.move(str(tmp_filepath), str(filepath))


def download_account_structure(api_client, account: CriteoAccount):
    """Downloads the criteo account structure for a given account

    Args:
        api_client: A pycriteo API client
        account: A Criteo account

    """
    logging.info(
        'downloading account structure report for account {account}'.format(account=account))
    advertiser_name = api_client.getAccount()['advertiserName']
    currency = api_client.getAccount()['currency']
    criteo_campaigns = api_client.getCampaigns(campaignSelector={})

    relative_filepath = Path('criteo-account-structure-{}-{version}.json.gz'.format(
        account.normalized_name,
        version=OUTPUT_FILE_VERSION))
    filepath = abspath(ensure_data_directory(relative_filepath))
    account_structure = []
    for campaign in criteo_campaigns:
        for single_campaign in campaign[1]:
            account_structure.append(
                map_account_structure(single_campaign, account, advertiser_name, currency))
    write_account_structure_data_to_json(account_structure, filepath=filepath)


def schedule_report_jobs(api_client) -> [int]:
    """ Triggers a Criteo report

    Args:
        api_client: A pycriteo API client

    Returns:
        A list of ids of scheduled criteo jobs

    """
    start_date = datetime.strptime(config.first_date(), '%Y-%m-%d')
    end_date = datetime.now() - timedelta(days=1)
    Datechunk = namedtuple('Datechunk', 'start_date, end_date')
    date_chunks = []
    current_date = start_date
    # Criteo has a max of 90 days to download the daily report,
    # so we have to download the data in 90 days intervals
    job_ids = []
    while current_date < end_date:
        date_chunks.append(Datechunk(datetime.strftime(current_date, '%Y-%m-%d'),
                                     datetime.strftime((current_date + timedelta(days=89)),
                                                       '%Y-%m-%d')))
        current_date = current_date + timedelta(days=90)

        for date_chunk in date_chunks:
            report_job = {
                'reportSelector': {},
                'reportType': 'Campaign',
                'aggregationType': 'Daily',
                'startDate': date_chunk.start_date,
                'endDate': date_chunk.end_date,
                'isResultGzipped': False
            }
            response = api_client.scheduleReportJob(reportJob=report_job)
            job_ids.append(response['jobID'])
    return job_ids


def is_job_completed(api_client, job_id: int) -> bool:
    """Checks if a scheduled report job is completed

    Args:
        api_client: A pycriteo API client
        job_id: The id of a scheduled report

    Returns:
        True if a scheduled job is completed, false if it has not

    """
    response = api_client.getJobStatus(job_id)
    if response == 'Completed':
        return True
    elif response == 'Pending':
        return False
    else:
        raise ValueError('Unknown job status received: {}'.format(response))


def _recursive_asdict(d) -> {}:
    """Convert an arbitrary object into a dictioary.

    Args:
        d: An arbitrary object

    Returns:
        A dictionary containing the object data
    """
    out = {}
    for k, v in asdict(d).items():
        if hasattr(v, '__keylist__'):
            out[k] = _recursive_asdict(v)
        elif isinstance(v, list):
            out[k] = []
            for item in v:
                if hasattr(item, '__keylist__'):
                    out[k].append(_recursive_asdict(item))
                else:
                    out[k].append(item)
        else:
            out[k] = v
    return out


def _suds_to_dict(data) -> {}:
    """Convert a suds object into a dictioary.

    Args:
        data: A suds object returned from the Criteo API

    Returns:
        A dictionary containing the suds object data

    """
    json_data = _recursive_asdict(data)
    return json_data


def map_account_structure(account_structure: object, account: CriteoAccount,
                          advertiser_name: str, currency: str) -> {}:
    """
    Args:
        account_structure: A suds object containing the Criteo account structure data
        account: A Criteo account
        advertiser_name: An advertisers name

    Returns:
        A dictionary with the account structure data

    """
    account_structure = _suds_to_dict(account_structure)
    account_structure['platform'] = account.platform
    account_structure['channel'] = account.channel
    account_structure['partner'] = account.partner
    account_structure['advertiserName'] = advertiser_name
    account_structure['currency'] = currency
    return account_structure


def write_account_structure_data_to_json(account_structure_data: [], filepath: Path):
    """Write the account structure data to a json file

    Args:
        account_structure_data: The criteo account structure data
        filepath: The path and name of the output file

    """
    json_data = json.dumps(account_structure_data)
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_filepath = Path(tmp_dir, filepath)
        with gzip.open(str(tmp_filepath), 'wt') as tmp_campaign_structure_file:
            tmp_campaign_structure_file.write(json_data)

        shutil.move(str(tmp_filepath), str(filepath))


def ensure_data_directory(relative_path: Path = None) -> Path:
    """Checks if a directory in the data dir path exists. Creates it if necessary

    Args:
        relative_path: A Path object pointing to a file relative to the data directory

    Returns:
        The absolute path Path object

    """
    if relative_path is None:
        return Path(config.data_dir())
    try:
        path = Path(config.data_dir(), relative_path)
        # if path points to a file, create parent directory instead
        if path.suffix:
            if not path.parent.exists():
                path.parent.mkdir(exist_ok=True, parents=True)
        else:
            if not path.exists():
                path.mkdir(exist_ok=True, parents=True)
        return path
    except OSError as exception:
        if exception.errno != errno.EEXIST:
            raise
