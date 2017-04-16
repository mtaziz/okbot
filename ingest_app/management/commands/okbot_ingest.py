# -*- coding: utf-8 -*-
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
import jieba.posseg as pseg
import collections
import json
import time
import os
import re

from ingest_app.models import Joblog
from utils import PsqlQuery, Tokenizer



import logging
logger = logging.getLogger('okbot_ingest')
logger.setLevel(logging.INFO)
ch = logging.StreamHandler()
chformatter = logging.Formatter('%(asctime)s %(levelname)s: %(message)s', datefmt='[%d/%b/%Y %H:%M:%S]')
ch.setFormatter(chformatter)
logger.addHandler(ch)


class Command(BaseCommand):
    help = '''
           Ingest the crawled data into database.
           ex: python manage.py okbot_ingest --jlpath <jsonline-file> --tokenizer <tokenizer>
           '''

    # idx_post = {
    #     'pk': 0,
    #     'title': 1,
    #     'tag': 2,
    #     'spider': 3,
    #     'url': 4,
    #     'author': 5,
    #     'push': 6,
    #     'publish_date': 7,
    #     'last_update': 8,
    #     'update_count': 9,
    #     'allow_update': 10
    # }

    # idx_vocab = {
    #     'pk': 0,
    #     'name': 1,
    #     'word': 2,
    #     'tokenizer': 3,
    #     'tag': 4,
    #     'doc_freq': 5,
    #     'excluded': 6
    # }

    # idx_vocab2post = {
    #     'pk': 0,
    #     'vocabulary_pk': 1,
    #     'post_pk': 2,
    # }

    # idx_grammar = {
    #     'pk': 0,
    #     'name': 1,
    #     'sent_tag': 2,
    #     'tokenizer': 3,
    #     'doc_freq': 4
    # }


    def add_arguments(self, parser):
        parser.add_argument('--jlpath', nargs=1, type=str)
        parser.add_argument('--tokenizer', nargs=1, type=str)

    def handle(self, *args, **options):
        jlpath, tok_tag = options['jlpath'][0], options['tokenizer'][0]
        
        file_name = jlpath.split('/')[-1]
        spider_tag = file_name[: file_name.find('.')]
        tokenizer = Tokenizer(tok_tag)

        time_tic = time.time()
        logger.info('okbot ingest job start. source: {}'.format(file_name))
        now = timezone.now()
        jobid =  '.'.join(file_name.split('.')[:3] + [now.strftime('%Y-%m-%d-%H-%M-%S')])
        Joblog(name=jobid, start_time=now, status='running').save()

        jlparser = CrawledJLParser(jlpath, tokenizer)
        ingester = Ingester(spider_tag)

        for batch_post in jlparser.batch_parse():
            ingester.upsert_post(batch_post)
            break
#            self._upsert_post(batch_post)
#            title_tok = [p['title_tok'] for p in batch_post]
            
#            vocabs = [item for sublist in title_tok for item in sublist]
#            tok_name = list({'--+--'.join([v.word, v.flag, self.tok_tag]) for v in vocabs})
#            self._upsert_vocab_ignore_df(batch_post, tok_name)
#            self._upsert_vocab2post(batch_post, tok_name)

#            post_sent_tag = [p['title_grammar'] for p in batch_post]
#            sent_tag = list({s for s in post_sent_tag})
#            self._upsert_grammar_ignore_df(batch_post, sent_tag)
#            self._upsert_grammar2post(batch_post, sent_tag)

        # self.cur.close()
        # self.conn.close()
        logger.info('okbot ingest job finished. elapsed time: {} sec.'.format(time.time() - time_tic))


        now = timezone.now()
        try:
            job = Joblog.objects.get(name=jobid)
            job.finish_time = now
        except Exception as e:
            logger.error(e)
            logger.error('command okbot_ingest, fail to fetch job log. id: {}. create a new one'.format(jobid))
            # try:
            job = Joblog(name=jobid, start_time=now)
            # except Exception as e:
                # logger.error(e)
                # logger.error('command okbot_ingest, fail to create job log')
                # return
        finally:
            job.status = 'finished'
            job.save()


class Ingester(object):
    query_post_sql = '''
        SELECT * FROM ingest_app_post WHERE url IN %s;
    '''
    upsert_post_sql = '''
        INSERT INTO ingest_app_post(title, tokenized, grammar, tag, spider, url, 
                                    author, push, publish_date, last_update, update_count, allow_update)
        SELECT unnest( %(title)s ), unnest( %(tokenized)s ), unnest( %(grammar)s ), 
               unnest( %(tag)s ), unnest( %(spider)s ), unnest( %(url)s ), unnest( %(author)s ), 
               unnest( %(push)s ), unnest( %(publish_date)s ), unnest( %(last_update)s ), 
               unnest( %(update_count)s ), unnest( %(allow_update)s )
        ON CONFLICT (url) DO 
        UPDATE SET 
            tokenized = EXCLUDED.tokenized,
            grammar = EXCLUDED.grammar,
            push = EXCLUDED.push,
            last_update = EXCLUDED.last_update,
            allow_update = EXCLUDED.allow_update,
            update_count = ingest_app_post.update_count + 1 
        WHERE ingest_app_post.allow_update = True;
    '''

    def __init__(self, tag):
        self.spider_tag = tag

    def query_post(self, url):
        psql = PsqlQuery()
        qpost = list(psql.query(self.query_post_sql, (tuple(url),)))
        schema = psql.schema
        return qpost, schema


    def upsert_post(self, batch_post):
        post_num = len(batch_post)
        
        title = [p['title'] for p in batch_post]
        tokenized = [p['title_tok'] for p in batch_post]
        grammar = [p['title_grammar'] for p in batch_post]
        url = [p['url'] for p in batch_post]
        tag = [p['tag'] for p in batch_post]
        author = [p['author'] for p in batch_post]
        push = [p['push'] for p in batch_post]
        publish_date = [p['date'] for p in batch_post]
        spider = [self.spider_tag] * post_num
        last_update = [timezone.now()] * post_num
        update_count = [1] * post_num
        allow_update = [True] * post_num

        # qpost, schema = self.query_post(url)
        # for i, q in enumerate(qpost):
        #     if q:
        #         if len(q[schema['push']]) == len(push[i]):
        #             allow_update[i] = False

        psql = PsqlQuery()
        psql.upsert(self.upsert_post_sql, locals())
        

        



class CrawledJLParser(object):

    def __init__(self, jlpath, tokenizer):
        self.jlpath = jlpath
        self.tokenizer = tokenizer

    def batch_parse(self, batch_size=1000):
        with open(self.jlpath, 'r') as f:
            i = 0
            parsed = [None] * batch_size
            for line in f:
                parsed[i] = self._parse(line)
                i += 1
                if i >= batch_size:
                    i = 0
                    parsed = [None] * batch_size
                    yield [ps for ps in parsed if ps]

            yield [ps for ps in parsed[:i] if ps]


    def _parse(self, line):
        try:
            post = json.loads(line)
            title_ = post['title']
            m = re.search('[\]|］]', title_)
            if m is None:
                title = title_.strip()
                tag = ''
            else:
                right_quote_idx = m.start()
                title = title_[right_quote_idx + 1 :].strip()
                tag = title_[1 : right_quote_idx].strip()

            title_tok, title_grammar = self.tokenizer.cut(title)
            return {
                'title': title,
                'tag': tag,
                'title_tok': ' '.join(title_tok),
                'title_grammar': ' '.join(title_grammar),
                'url': post['url'],
                'author': post['author'],
                'date': timezone.datetime.strptime(post['date'], '%a %b %d %H:%M:%S %Y'),
                'push': '\n'.join(post['push']),
            }

        except Exception as e:
            logger.warning(e)
            logger.warning('command okbot_ingest, jsonline record faild to parse in, ignored. line: {}'.format(line.encode('utf-8').decode('unicode-escape')))
            return {}
