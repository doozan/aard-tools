# -*- coding: utf-8 -*-
# This file is part of Aard Dictionary Tools <http://aarddict.org>.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 3
# as published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License <http://www.gnu.org/licenses/gpl-3.0.txt>
# for more details.
#
# Copyright (C) 2008-2009  Igor Tkach

from __future__ import with_statement
import functools
import logging
import os
from itertools import islice

try:
    import json
except ImportError:
    import simplejson as json

import yaml

from mwlib import uparser, xhtmlwriter, _locale
from mwlib.log import Log
Log.logfile = None

from mwlib import lrucache, expr
expr._cache = lrucache.mt_lrucache(100)

from mwlib.templ.evaluate import Expander
Expander.parsedTemplateCache = lrucache.lrucache(100)

tojson = functools.partial(json.dumps, ensure_ascii=False)

import multiprocessing
from multiprocessing import Pool, TimeoutError
from mwlib.cdb.cdbwiki import WikiDB
from mwlib._version import version as mwlib_version
import mwlib.siteinfo

import mwaardhtmlwriter as writer

import re

lic_dir = os.path.join(os.path.dirname(__file__), 'licenses')

known_licenses = {"Creative Commons Attribution-Share Alike 3.0 Unported": 
                  os.path.join(lic_dir, "ccasau-3.0.txt"),
                  "GNU Free Documentation License 1.2": 
                  os.path.join(lic_dir, "gfdl-1.2.txt")}

wikidb = None
log = logging.getLogger('wiki')

def _create_wikidb(cdbdir, lang, rtl, filters):
    global wikidb
    wikidb = Wiki(cdbdir, lang, rtl, filters)

def _init_process(cdbdir, lang, rtl, filters):
    global log
    log = multiprocessing.get_logger()
    _create_wikidb(cdbdir, lang, rtl, filters)

class ConvertError(Exception):

    def __init__(self, title):
        Exception.__init__(self, title)
        self.title = title

    def __str__(self):
        """
        >>> e = ConvertError('абвгд'.decode('utf8'))
        >>> print str(e)
        ConvertError: абвгд

        """
        return 'ConvertError: %s' % self.title.encode('utf8')

    def __repr__(self):
        """
        >>> e = ConvertError('абвгд'.decode('utf8'))
        >>> print repr(e)
        ConvertError: u'\u0430\u0431\u0432\u0433\u0434'

        """
        return 'ConvertError: %r' % self.title


class EmptyArticleError(ConvertError): pass

def mkredirect(title, redirect_target):
    meta = {u'r': redirect_target}
    return title, tojson(('', [], meta)), True, None

def convert(title):
    try:
        text = wikidb.reader[title]
        size = wikidb.reader.getitem_size(title)

        if not text:
            raise EmptyArticleError(title)

        redirect = wikidb.get_redirect(text)
        if redirect:
            return mkredirect(title, redirect) + ( size, )

        mwobject = uparser.parseString(title=title,
                                       raw=text,
                                       wikidb=wikidb,
                                       lang=wikidb.lang,
                                       magicwords=wikidb.siteinfo['magicwords'])
        xhtmlwriter.preprocess(mwobject)
        text, tags, languagelinks = writer.convert(mwobject, wikidb.rtl, wikidb.filters)

        if ( len(wikidb.filters['REGEX']) > 0):
          for item in wikidb.filters['REGEX']:
            text = item['re'].sub( item['sub'], text )

    except EmptyArticleError:
        raise
    except Exception:
        log.exception('Failed to process article %s', title.encode('utf8'))
        raise ConvertError(title)
    else:
        return title, tojson((text.rstrip(), tags)), False, languagelinks, size


class BadRedirect(ConvertError): pass


def parse_redirect(text, aliases):
    """
    >>> aliases = [u"#PATRZ", u"#PRZEKIERUJ", u"#TAM", u"#REDIRECT"]
    >>> parse_redirect(u'#PATRZ [[Zmora]]', aliases)
    u'Zmora'
    >>> parse_redirect(u'#PATRZ[[Zmora]]', aliases)
    u'Zmora'
    >>> parse_redirect(u'#PRZEKIERUJ [[Uwierzytelnianie]]', aliases)
    u'Uwierzytelnianie'

    >>> parse_redirect('#TAM[[Żuraw samochodowy]]'.decode('utf8'), aliases)
    u'\u017buraw samochodowy'

    >>> parse_redirect('#перенапр[[абв]]'.decode('utf8'), [u"#\u043f\u0435\u0440\u0435\u043d\u0430\u043f\u0440"])
    u'\u0430\u0431\u0432'

    >>> parse_redirect('#Перенапр[[абв]]'.decode('utf8'),
    ...                [u"#\u043f\u0435\u0440\u0435\u043d\u0430\u043f\u0440",
    ...                 u"#\u043f\u0435\u0440\u0435\u043d\u0430\u043f\u0440".upper()])
    u'\u0430\u0431\u0432'

    >>> parse_redirect(u'abc', aliases)

    >>> parse_redirect(u'#REDIRECT [[abc', aliases)
    Traceback (most recent call last):
    ...
    BadRedirect: ConvertError: [[abc

    >>> parse_redirect(u'#REDIRECT abc', aliases)
    Traceback (most recent call last):
    ...
    BadRedirect: ConvertError: abc

    >>> parse_redirect('#REDIRECT абв'.decode('utf8'), aliases)
    Traceback (most recent call last):
    ...
    BadRedirect: ConvertError: абв

    """
    for alias in aliases:
        if text.startswith(alias) or text.upper().startswith(alias):
            text = text[len(alias):].lstrip()
            begin = text.find('[[')
            if begin < 0:
                raise BadRedirect(text)
            end = text.find(']]')
            if end < 0:
                raise BadRedirect(text)
            return text[begin+2:end]
    return None

class Wiki(WikiDB):

    def __init__(self, cdbdir, lang, rtl, filters):
        WikiDB.__init__(self, cdbdir, lang=lang)
        self.lang = lang
        self.rtl = rtl
        self.redirect_aliases = set()
        aliases = [magicword['aliases']
                                 for magicword in self.siteinfo['magicwords']
                                 if magicword['name'] == 'redirect'][0]

        for alias in aliases:
            self.redirect_aliases.add(alias)
            self.redirect_aliases.add(alias.lower())
            self.redirect_aliases.add(alias.upper())

        self.filters = filters

    def get_redirect(self, text):
        redirect = parse_redirect(text, self.redirect_aliases)
        if redirect:
            redirect = self.nshandler.get_fqname(redirect)
        return redirect

    def getURL(self, title):
        return ''

    def getSource(self, title, revision=None):
        from mwlib.metabook import make_source

        g = self.siteinfo['general']
        return make_source(
            name='%s (%s)' % (g['sitename'], g['lang']),
            url=g['base'],
            language=g['lang'],
            base_url=self.nfo['base_url'],
            script_extension=self.nfo['script_extension'],
        )

    def get_page(self,  name,  revision=None):
        if (name in self.filters['EXCLUDE_PAGES']):
          return
        else:
          return WikiDB.get_page(self, name, revision)

    def normalize_and_get_page(self, name, defaultns):
        fqname = self.nshandler.get_fqname(name, defaultns=defaultns)
        return self.get_page(fqname)

    def normalize_and_get_image_path(self, name):
        assert isinstance(name, basestring)
        name = unicode(name)

        ns, partial, fqname = self.nshandler.splitname(name, defaultns=6)
        if ns != 6:
            return

        if "/" in fqname:
            return None

    def articles_sizes(self):
        for a,s in self.reader.iteritems_sizes():
            nsnum = self.nshandler.splitname(a)[0]
            if nsnum==0:
                yield a,s

import sys

def total(inputfile, options):
    load_siteinfo(options.siteinfo)
    w = Wiki(inputfile, options.wiki_lang, options.rtl, options.filters)
    articles = 0
    total_bytes = 0
    for (article, size) in islice(w.articles_sizes(), options.start, options.end):
        articles += 1
        total_bytes += size
        if (articles % 10000 == 0):
            sys.stdout.write('\033[2KCalculating total number of articles...(%d, %d)\r' % (articles, total_bytes))
            sys.stdout.flush()
        pass
    try:
        return articles,total_bytes
    except:
        return 0,0


def make_input(input_file_name):
    return input_file_name

def collect_articles(input_file, options, compiler):
    p = WikiParser(options, compiler)
    p.parse(input_file)

siteinfo_loaded = False

def load_siteinfo(filename):
    global siteinfo_loaded
    if siteinfo_loaded:
        return mwlib.siteinfo.get_siteinfo(None)
    if not filename:
        raise Exception('Site info not specified (fetch with aard-siteinfo, '
                        'specify with use --siteinfo)')

    if not os.path.exists(filename):
        raise Exception('File %s not found' % filename)

    with open(filename) as f:
        siteinfo = json.load(f)
        siteinfo_loaded = True

    mwlib.siteinfo.get_siteinfo = lambda lang: siteinfo

    return siteinfo

def load_filters(filename):
    if not filename:
        raise Exception('Site filter not specified'
                        'specify with use --filters)')

    if not os.path.exists(filename):
        raise Exception('File %s not found' % filename)

    with open(filename) as f:
        filters = yaml.load(f)

    for filter_section in ['EXCLUDE_PAGES', 'EXCLUDE_CLASSES', 'EXCLUDE_IDS', 'TEXT_REPLACE']:
      if (filter_section not in filters or filters[filter_section] is None):
        filters[filter_section] = []

    filters['REGEX'] = []
    if (len(filters['TEXT_REPLACE']) > 0):
      for item in filters['TEXT_REPLACE']:
         sub = ""
         if ('sub' in item):
           sub = item['sub']
         filters['REGEX'].append( { "re": re.compile(item['re']), "sub": sub } )

    return filters

default_description = """ %(title)s for Aard Dictionary is a collection of text documents from %(server)s (articles only). Some documents or portions of documents may have been omited or could not be converted to Aard Dictionary format. All documents can be found online at %(server)s under the same title as displayed in Aard Dictionary.
"""

class WikiParser():

    def __init__(self, options, consumer):
        self.consumer = consumer
        wiki_lang = options.wiki_lang
        siteinfo = load_siteinfo(options.siteinfo)
        self.filters = load_filters(options.filters)

        consumer.add_metadata('siteinfo', siteinfo)
        general_siteinfo = siteinfo['general']
        sitename = general_siteinfo['sitename']
        sitelang = general_siteinfo['lang']

        try:
            _locale.set_locale_from_lang(sitelang.encode('latin-1'))
        except BaseException, err:
            print "Error: could not set locale", err
            print "Class: ", sitelang.__class__

        from ConfigParser import ConfigParser
        c = ConfigParser()

        if options.metadata:
            read_metadata_files = c.read(options.metadata)
            if not read_metadata_files:
                log.warn('Metadata file could not be read %s' % options.metadata)
            else:
                log.info('Using metadata from %s', ', '.join(read_metadata_files))
                for opt in c.options('metadata'):
                    value = c.get('metadata', opt)
                    self.consumer.add_metadata(opt, value)
        else:
            log.warn('No metadata file specified')


        if options.license:
            license_file = options.license
        else:
            rights = general_siteinfo['rights']
            if rights in known_licenses:
                license_file = known_licenses[rights]
            else:
                license_file = None
                self.consumer.add_metadata('license', rights)

        if license_file:
            with open(license_file) as f:
                log.info('Using license text from %s', license_file)            
                license_text = f.read()
                self.consumer.add_metadata('license', license_text)            
                
        if options.copyright:
            copyright_file = options.copyright
            with open(copyright_file) as f:
                log.info('Using copyright text from %s', copyright_file)            
                copyright_text = f.read()
                self.consumer.add_metadata('copyright', copyright_text)                        

        self.consumer.add_metadata("title", sitename)
        if options.dict_ver:
            self.consumer.add_metadata("version", 
                                       "-".join((options.dict_ver, 
                                                 options.dict_update)))
        server = general_siteinfo['server']
        self.consumer.add_metadata("source", server)
        self.consumer.add_metadata("description", default_description % dict(server=server, 
                                                                             title=sitename))

        self.lang = wiki_lang
        self.rtl = options.rtl
        self.consumer.add_metadata("lang", wiki_lang)
        self.consumer.add_metadata("sitelang", sitelang)
        self.consumer.add_metadata("index_language", sitelang)
        self.consumer.add_metadata("article_language", sitelang)
        log.info('Language: %s (%s)', self.lang, sitelang)

        self.consumer.add_metadata('mwlib',
                                   '.'.join(str(v) for v in mwlib_version))
        self.processes = options.processes if options.processes else None
        self.pool = None
        self.timeout = options.timeout
        self.timedout_count = 0
        self.start = options.start
        self.end = options.end
        if options.nomp:
            log.info('Disabling multiprocessing')
            self.parse = self.parse_simple
        else:
            self.parse = self.parse_mp
        self.mp_chunk_size = options.mp_chunk_size

        if options.lang_links:
            self.lang_links_langs = frozenset(l.strip().lower()
                                              for l in options.lang_links.split(',')
                                              if l.strip().lower() != sitelang)
            self.consumer.add_metadata("language_links", list(self.lang_links_langs))
        else:
            self.lang_links_langs = frozenset()

        self.requested_article_count = options.article_count


    def articles(self, f):
        if self.start > 0:
            log.info('Skipping to article %d', self.start)
        _create_wikidb(f, self.lang, self.rtl, self.filters)
        for title in islice(wikidb.articles(), self.start, self.end):
            log.debug('Yielding "%s" for processing', title.encode('utf8'))
            yield title

    def articles_sizes(self, f):
        if self.start > 0:
            log.info('Skipping to article %d', self.start)
        _create_wikidb(f, self.lang, self.rtl, self.filters)
        for (title, size) in islice(wikidb.articles_sizes(), self.start, self.end):
            log.debug('Yielding "%s" for processing', title.encode('utf8'))
            yield (title,size)

    def reset_pool(self, cdbdir, terminate=True):
        if self.pool and terminate:
            log.info('Terminating current worker pool')
            self.pool.terminate()
        log.info('Creating new worker pool with wiki cdb at %s', cdbdir)

        self.pool = Pool(processes=self.processes,
                         initializer=_init_process,
                         initargs=[cdbdir, self.lang, self.rtl, self.filters])

    def parse_simple(self, f):
        _init_process(f, self.lang, self.rtl, self.filters)
        self.consumer.add_metadata('article_format', 'html')
        articles = self.articles(f)
        for a in articles:
            try:
                result = convert(a)
                title, serialized, redirect, langugagelinks, size = result
                self.consumer.add_article(title, serialized, redirect, True, size)
                self.process_languagelinks(title, langugagelinks)
            except EmptyArticleError, e:
                self.consumer.empty_article(e.title)
            except ConvertError, e:
                self.consumer.fail_article(e.title)

    def parse_mp(self, f):
        try:
            self.consumer.add_metadata('article_format', 'html')
            articles = self.articles(f)
            self.reset_pool(f)
            iter_count = 1
            real_article_count = 0
            while True:
                if iter_count:
                    chunk = islice(articles, self.mp_chunk_size)
                    iter_count = 0
                else:
                    break

                resulti = self.pool.imap_unordered(convert, chunk)
                while True:
                    try:
                        result = resulti.next(self.timeout)
                        iter_count += 1
                        title, serialized, redirect, langugagelinks, size  = result

                        if self.requested_article_count:
                            if  not redirect:
                                real_article_count += 1
                                self.consumer.add_article(title, serialized, redirect, True, size)
                                self.process_languagelinks(title, langugagelinks)
                                if real_article_count >= self.requested_article_count:
                                    try:
                                        self.pool.terminate()
                                    except:
                                        log.exception()
                                    finally:
                                        return
                        else:
                            self.consumer.add_article(title, serialized, redirect, True, size)
                            self.process_languagelinks(title, langugagelinks)
                    except StopIteration:
                        break
                    except TimeoutError:
                        log.warn('Worker pool timed out')
                        self.consumer.timedout(count=len(multiprocessing.active_children()))
                        self.reset_pool(f)
                        resulti = self.pool.imap_unordered(convert, chunk)
                    except AssertionError:
                        log.exception()
                    except EmptyArticleError, e:
                        self.consumer.empty_article(e.title)
                    except ConvertError, e:
                        self.consumer.fail_article(e.title)
                    except KeyboardInterrupt:
                        log.error('Keyboard interrupt: '
                                  'terminating worker pool')
                        self.pool.terminate()
                        raise

                self.pool.close()
                self.reset_pool(f, terminate=False)
        finally:
            self.pool.close()
            self.pool.join()

    def process_languagelinks(self, title, languagelinks):
        if not languagelinks:
            return
        targets = set()
        for namespace, target in languagelinks:
            if namespace in self.lang_links_langs:
                log.debug('Language link for %s: %s (%s)',
                          title.encode('utf8'), target.encode('utf8'),
                          namespace.encode('utf8'))
                i = target.find(namespace+u':')
                if i > -1:
                    unqualified_target = target[len(namespace)+1:]
                    try:
                        wikidb.reader[unqualified_target]
                    except KeyError:
                        targets.add(unqualified_target)
                else:
                    log.warn('Invalid language link "%s"', target.encode('utf8'))
        for target in targets:
            (l_title, l_serialized,
             l_redirect, l_langugagelinks) = mkredirect(wikidb.nshandler.get_fqname(target), title)
            self.consumer.add_article(l_title, l_serialized,
                                      redirect=True, count=False)

