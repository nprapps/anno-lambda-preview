#!/usr/bin/env python
# _*_ coding:utf-8 _*_
import logging
import app_config
import boto3
import csv
import gzip
from StringIO import StringIO
from copydoc import CopyDoc
import parse_doc
from jinja2 import Environment, FileSystemLoader


s3 = boto3.resource('s3')
bucket = s3.Bucket(app_config.BUCKET)

logger = logging.getLogger()
logger.setLevel(app_config.LOG_LEVEL)
logging.getLogger('authomatic').setLevel(logging.WARNING)
logging.getLogger('boto3').setLevel(logging.WARNING)
logging.getLogger('botocore').setLevel(logging.WARNING)

env = Environment(loader=FileSystemLoader('templates/factcheck'))


def transform_authors(authors_data):
    """
    transform authors received csv data to final authors dict
    """
    authors = {}
    try:
        cr = csv.DictReader(authors_data.split('\n'))
        for row in cr:
            if row['initials'] in authors:
                logger.warning("Duplicate initials on authors dict: %s" % (
                    row['initials']))
            authors[row['initials']] = row
    except Exception, e:
        logger.error("Could not transform the authors data: %s" % (e))
    finally:
        return authors


def upload_template_contents(context, template, s3filename=None):
    """
    populates jinja2 template
    and uploads to s3
    """
    if not s3filename:
        s3filename = template
    template = env.get_template(template)
    markup = template.render(**context)
    f = StringIO()
    with gzip.GzipFile(fileobj=f, mode='w', mtime=0) as gz:
        gz.write(markup)
    # Reset buffer to beginning
    f.seek(0)
    s3Key = '%s%s/%s' % (app_config.FACTCHECKS_DIRECTORY_PREFIX,
                         app_config.PREVIEW_FACTCHECK,
                         s3filename)
    bucket.put_object(Key=s3Key,
                      Body=f.read(),
                      ContentType='text/html',
                      ContentEncoding='gzip',
                      CacheControl='max-age=%s' % app_config.DEFAULT_MAX_AGE)


def lambda_handler(event, context):
    """
    Retrieves drive keys from the request payload
    - connects to google using authomatic and OAuth2 credentials
    - parses the factcheck document and publishes to staging
    """
    try:
        try:
            logger.info('Start preview generation')
            TRANSCRIPT_GDOC_KEY = event['doc_key']
            AUTHORS_GOOGLE_DOC_KEY = event['authors_key']
        except KeyError:
            msg = 'Did not find needed params in %s' % (event)
            raise app_config.UserException('[BadRequest]: %s' % msg)
        authors_url = app_config.SPREADSHEET_URL_TEMPLATE % (
            AUTHORS_GOOGLE_DOC_KEY)
        doc_url = app_config.DOC_URL_TEMPLATE % (
            TRANSCRIPT_GDOC_KEY)

        # Get the credentials and refresh if necesary
        credentials = app_config.authomatic.credentials(
            app_config.GOOGLE_CREDS)
        # Refresh credentials if needed
        if not credentials.valid:
            credentials.refresh()

        # Get authors
        response = app_config.authomatic.access(credentials, authors_url)
        if response.status != 200:
            msg = 'While accessing %s got HTTP: %s' % (authors_url,
                                                       response.status)
            raise app_config.UserException('[BadRequest]: %s' % msg)
        authors_data = response.content
        authors = transform_authors(authors_data)
        if not authors:
            msg = 'Could not parse authors spreadsheet %s' % (authors_url)
            raise app_config.UserException('[BadRequest]: %s' % msg)
        # Get doccument
        response = app_config.authomatic.access(credentials, doc_url)
        if response.status != 200:
            msg = 'While accessing %s got HTTP: %s' % (doc_url,
                                                       response.status)
            raise app_config.UserException('[BadRequest]: %s' % msg)
        html = response.content

        # Parse data
        doc = CopyDoc(html)
        logger.info('Parsed doc html with copydoc')
        context = parse_doc.parse(doc, authors)
        logger.info('Parsed factcheck')

        # Generate final files and upload to S3
        upload_template_contents(context, 'factcheck.html')
        upload_template_contents(context, 'share.html')
        context['preview'] = True
        upload_template_contents(context, 'factcheck.html',
                                 'factcheck_preview.html')
        logger.info('Generated factcheck templates. Execution successful')
        return {'message': 'Preview generated successfully'}
    except app_config.UserException, e:
        logger.error('Exit with controlled exception %s' % e)
        raise
    except Exception, e:
        if isinstance(e, app_config.UserException):
            raise
        else:
            logger.error('Failed lambda function. reason: %s' % (e))
            raise Exception('[InternalServerError]: Unexpected Error')
