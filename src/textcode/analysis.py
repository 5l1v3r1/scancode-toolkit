#
# Copyright (c) 2015 nexB Inc. and others. All rights reserved.
# http://nexb.com and https://github.com/nexB/scancode-toolkit/
# The ScanCode software is licensed under the Apache License version 2.0.
# Data generated with ScanCode require an acknowledgment.
# ScanCode is a trademark of nexB Inc.
#
# You may not use this software except in compliance with the License.
# You may obtain a copy of the License at: http://apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software distributed
# under the License is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR
# CONDITIONS OF ANY KIND, either express or implied. See the License for the
# specific language governing permissions and limitations under the License.
#
# When you publish or redistribute any data created with ScanCode or any ScanCode
# derivative work, you must accompany this data with the following acknowledgment:
#
#  Generated with ScanCode and provided on an "AS IS" BASIS, WITHOUT WARRANTIES
#  OR CONDITIONS OF ANY KIND, either express or implied. No content created from
#  ScanCode should be considered or used as legal advice. Consult an Attorney
#  for any legal advice.
#  ScanCode is a free software code scanning tool from nexB Inc. and others.
#  Visit https://github.com/nexB/scancode-toolkit/ for support and download.

from __future__ import absolute_import, print_function

from collections import deque
import re

import unicodedata

import typecode.contenttype
from textcode import strings
from textcode import pdf
from textcode import markup

# Template constants
# Support template gaps up to 2 digits, 99 tokens maximum.
MAX_GAP = 150

# A gap when chunks of texts are contiguous, i.e. regular text, not a template
NO_GAP = 0

# Default template if no explicit gap is specified in a template
DEFAULT_GAP = 5

DEFAULT_NGRAM_LEN = 3


class Token(object):
    """
    A Token has position info and a value.
    A position tracks:
        * the absolute unigram position (start,end) that can overlap if tokens
          were ngrammed
        * the start line and start character on this start line
        * the end line and end character on this end line.
          Line numbering starts at 0.

    Gap is the maximum number tokens following this token that could be
    skipped. The fields order is chosen such that tokens will sort correctly.

    The methods semantic are the same as for a namedtuple. 
    """
    __slots__ = ('start', 'start_line', 'start_char',
                 'end_line', 'end_char', 'end',
                 'gap', 'value',)

    def __init__(self, start=0, start_line=0, start_char=0,
                       end_line=0, end_char=0, end=0,
                       gap=NO_GAP, value=None):
        self.start = start
        self.start_line = start_line
        self.start_char = start_char
        self.end_line = end_line or start_line
        self.end_char = end_char
        self.end = end or start
        self.gap = gap
        self.value = value

    def __repr__(self):
        return ('Token('
                'start=%(start)r, start_line=%(start_line)r, '
                'start_char=%(start_char)r, end_line=%(end_line)r, '
                'end_char=%(end_char)r, end=%(end)r, '
                'gap=%(gap)r, value=%(value)r'
                ')' % self._asdict())

    @staticmethod
    def from_other(other):
        if other.is_other(other):
            return other(other.start, other.start_line, other.start_char,
                         other.end_line, other.end_char, other.end,
                         other.gap, other.value)

    def copy(self):
        return Token.from_token(self)

    __copy__ = __deepcopy__ = copy

    def _replace(self, **kwargs):
        new_token = self.copy()
        for k, v in kwargs.items():
            if k in self.__slots__:
                setattr(new_token, k, v)
        return new_token

    def _asdict(self):
        return dict((k, getattr(self, k),) for k in self.__slots__)

    @staticmethod
    def is_token(other):
        return other and isinstance(other, Token)

    def __eq__(self, other):
        if Token.is_token(other):
            return all(getattr(self, s) == getattr(other, s)
                       for s in self.__slots__)

    def __ne__(self, other):
        if Token.is_token(other):
            return any(getattr(self, s) != getattr(other, s)
                       for s in self.__slots__)

    def __lt__(self, other):
        if Token.is_token(other):
            return self.start < other.start

    def __gt__(self, other):
        if Token.is_token(other):
            return self.tart > other.start

    def __hash__(self):
        return hash(tuple(getattr(self, s) for s in self.__slots__))

    def len(self):
        return self.end - self.start


# split on whitespace and punctuation: keep only characters using a (trick)
# double negation regex on characters (e.g. [^\W]) and the underscore
def word_splitter():
    return re.compile(r'[^\W_]+', re.UNICODE).finditer


# template-aware variation of word splitter, also keeping {{ and }} as tokens.
# use non capturing groups for alternation
def template_splitter():
    return re.compile(r'(?:[^\W_])+'
                      r'|(?:\{\{)'
                      r'|(?:\}\})', re.UNICODE).finditer


def unigram_splitter(lines, splitter=None, lowercase=True):
    """
    Given an iterable of UNICODE string lines, split each line in Tokens using
    the `splitter` function in unigrams (aka. words) and yield Tokens. Strings
    are optionally made `lowercase`.

    Line numbering starts at 0.
    """
    if not lines:
        return

    splitter = splitter or word_splitter()
    for line_num, line in enumerate(lines):
        if not line.strip():
            continue
        if lowercase:
            line = line.lower()
        for word in splitter(line):
            val = word.group()
            val = val.encode('utf-8')
            # NOTE we are interning strings
            val = intern(val)
            yield Token(start_line=line_num, start_char=word.start(),
                        end_char=word.end(), end_line=line_num,
                        value=val)


template_start = '{{'
template_end = '}}'


class UnbalancedTemplateError(Exception):
    pass


class InvalidGapError(Exception):
    pass


def template_processor(unigrams):
    """
    Process a `unigrams` iterable for templates parts (i.e. regions marked
    with {{ }}). Update the token preceding a template part with the extracted
    specified gap or a default gap. Remove template artifacts from the token
    stream such as {{,  intra- template tokens and }}. Yield the updated and
    filtered stream of unigrams. Raise 

    UnbalancedTemplateError or InvalidGapError for invalid templates.
    """
    previous = None
    for token in unigrams:
        if not token:
            continue

        # not a template start: yield previous and keep current as previous
        if token.value != template_start:
            if previous and previous.value != template_end:
                yield previous
            previous = token
            continue

        # a template start: parse the token stream as a template
        try:
            token = unigrams.next()
        except StopIteration:
            raise UnbalancedTemplateError()

        # next token is either a gap, an intra-template token or a template_end
        if token.value == template_end:
            # template is : {{}}, use default gap, yield and continue
            # if there is no previous, this is a template at the start of
            # stream which does not have any meaning, so we skip it too
            if previous:
                previous.gap = DEFAULT_GAP
                yield previous
                previous = None
            continue

        # we are now inside a template at first token: is this a gap number?
        # compute gap, yield previous and reset previous
        try:
            gap = int(token.value)
            if gap > MAX_GAP:
                raise InvalidGapError('Requested gap: %d, maximum gap:%d' % (gap, MAX_GAP))
            # if yes, use specified gap
            if previous:
                previous.gap = gap
                yield previous
        except ValueError:
            # if no gap specified, use default gap
            if previous:
                previous.gap = DEFAULT_GAP
                yield previous
        # reset previous since we yielded and are going out of a template
        previous = None

        # finally skip intra-template tokens until we hit template_end
        try:
            while token.value != template_end:
                token = unigrams.next()
                # invalid nested template
                if token.value == template_start:
                    raise UnbalancedTemplateError()
        except StopIteration:
            # no template end
            raise UnbalancedTemplateError()

    # yield the last token
    if previous:
        yield previous


def position_processor(tokens):
    """
    Process an iterable of tokens and add start and end position attributes to
    each token.
    """
    for pos, token in enumerate(tokens):
        if not token:
            continue
        token.start = pos
        token.end = pos
        yield token


def merge_tokens(tokens):
    """
    Given a sequence of Tokens (typically a tuple), return a new merged Token
    computed from the first and last Token. Ignore gaps. Combine values in a
    tuple of values. Does not check if Tokens are sorted, contiguous or
    overlapping.
    """
    first = tokens[0]
    last = tokens[-1]
    if first is last:
        # unigram
        values = (first.value,)
    else:
        values = tuple([t.value for t in tokens])

    return Token(start=first.start, end=last.end,
                 start_line=first.start_line, start_char=first.start_char,
                 end_line=last.end_line, end_char=last.end_char,
                 value=values)


def multigrams_processor(unigrams, ngram_len):
    """
    Given a sequence or iterable of unigram Tokens, return an iterator of of
    Tokens containing the tokens Tokens for every length from 1 (e.g. unigrams)
    to length ngram_len. Buffers at most ngram_len tokens.
    
    For example, with these tokens [1, 2, 3, 4, 5] and ngram_len 3, these token
    are returned::

    >>> u=[Token(value=x) for x in range(1,6)]
    >>> from pprint import pprint
    >>> pprint(list(t.value for t in multigrams_processor(u,3)))
    [(1,),
     (1, 2),
     (1, 2, 3),
     (2,),
     (2, 3),
     (2, 3, 4),
     (3,),
     (3, 4),
     (3, 4, 5),
     (4,),
     (4, 5),
     (5,)]

    And with ngram_len 4, these token are returned::

    >>> pprint(list(t.value for t in multigrams_processor(u,4)))
    [(1,),
     (1, 2),
     (1, 2, 3),
     (1, 2, 3, 4),
     (2,),
     (2, 3),
     (2, 3, 4),
     (2, 3, 4, 5),
     (3,),
     (3, 4),
     (3, 4, 5),
     (4,),
     (4, 5),
     (5,)]
    """

    tokens = []
    for unigram in unigrams:
        if len(tokens) == ngram_len:
            for gr in multigrams(tokens):
                yield gr
            tokens.pop(0)
        tokens.append(unigram)
    while tokens:
        for gr in multigrams(tokens):
            yield gr
        tokens.pop(0)


def multigrams(grams):
    """
    Yield all merged token x, xy, xyz from the grams Token sequence.
    """
    tks = []
    for g in grams:
        tks.append(g)
        yield merge_tokens(tks)


def ngram_processor(iterable, ngram_len):
    """
    Given a sequence or iterable of arbitrary items, return an iterator of
    item ngrams tuples of length ngram_len. Buffers at most ngram_len iterable
    items.

    For example::

    >>> list(ngram_processor([1, 2, 3, 4, 5], 3))
    [(1, 2, 3), (2, 3, 4), (3, 4, 5)]
    """
    ngram = deque()
    current_len = 0
    for item in iterable:
        if current_len == ngram_len:
            yield tuple(ngram)
            ngram.popleft()
            current_len -= 1
        ngram.append(item)
        current_len += 1
    yield tuple(ngram)


def tokens_ngram_processor(tokens, ngram_len):
    """
    Given a `tokens` sequence or iterable of Tokens, return an iterator of
    tuples of Tokens where the tuples length is length `ngram_len`. Buffers at
    most `ngram_len` iterable items. The returned tuples contains always
    `ngram_len items`, possibly with None placeholder for ngrams shorter than
    `ngram_len`.

    Handles possible cases where the number of Tokens is smaller than
    `ngram_len`:
    - between the beginning of the stream and a first gap
    - between a last gap and the end of the stream
    - between two gaps
    In these cases, shorter ngrams can be returned.
    """
    ngram = deque()
    for token in tokens:
        if len(ngram) == ngram_len:
            yield tuple(ngram)
            ngram.popleft()
        if token.gap:
            ngram.append(token)
            yield tuple(ngram)
            # reset
            ngram.clear()
        else:
            ngram.append(token)
    if ngram:
        # yield last ngram
        yield tuple(ngram)


def ngram_to_token(iterable):
    """
    Return an iterable of single merged Tokens, merging the tuple's Tokens in
    one Token, given an `iterable` of ngram Tokens tuples from
    tokens_ngram_processor. The resulting Token value is always a tuple of
    values (and possibly a tuple with a single value for unigrams).
    """
    for ngram in iterable:
        token = ngram[0]
        last = ngram[-1]
        token.end_line = last.end_line
        token.end_char = last.end_char
        token.end = last.end
        # keep last gap: intra-ngram gap has no meaning
        token.gap = last.gap
        token.value = tuple(t.value for t in ngram)
        yield token


tsplitter = template_splitter()
wsplitter = word_splitter()


def unigram_tokenizer(lines, template=False):
    """
    Return an iterable of unigram Tokens (aka. single words) computed from an
    iterable of UNICODE string `lines`.
    Treat the `lines` strings as templated if `template` is True.
    """
    # TODO: consider handling de-hyphenation for hyphens and em-dash at end of
    # lines.
    if not lines:
        return

    if template:
        splitter = tsplitter
    else:
        splitter = wsplitter
    unigrams = unigram_splitter(lines, splitter)

    if template:
        unigrams = template_processor(unigrams)
    unigrams = position_processor(unigrams)
    return unigrams


def ngram_tokenizer(lines, ngram_len=DEFAULT_NGRAM_LEN, template=False):
    """
    Return an iterable of ngram Tokens of ngram length  `ngram_len` computed
    from the `lines` iterable of UNICODE strings.
    Treat the `lines` strings as templated if `template` is True.
    """
    if not lines:
        return

    ngrams = unigram_tokenizer(lines, template)
    ngrams = tokens_ngram_processor(ngrams, ngram_len)
    ngrams = ngram_to_token(ngrams)
    return ngrams


def multigram_tokenizer(lines, ngram_len=DEFAULT_NGRAM_LEN):
    """
    Return an iterable of ngram Tokens of every ngram length from 1 to
    `ngram_len` computed from the `lines` iterable of UNICODE strings.
    """
    if not lines:
        return
    unigrams = unigram_tokenizer(lines, template=False)
    multigrams = multigrams_processor(unigrams, ngram_len)
    return multigrams


def doc_subset(lines, position):
    """
    Return an iterable of `lines` that is the subset of `lines` and characters
    defined by the `position`.

    Line numbering starts at 0.
    """
    if lines:
        for ln , line in enumerate(lines):
            # single line case
            if ln == position.start_line == position.end_line:
                yield line[position.start_char:position.end_char]
            # here the position spans lines
            elif ln == position.start_line:
                yield line[position.start_char:]
            elif ln == position.end_line:
                yield line[:position.end_char]
            elif position.start_line < ln < position.end_line:
                yield line


def text_lines(location):
    """
    Return a text lines iterator from file at `location`. Return an empty iterator if no text content is
    extractible.
    Text extraction is based on detected file type

    Note: For testing or building from strings, location can be a is a list of
    unicode line strings.
    """
    if not location:
        return iter([])

    if not isinstance(location, basestring):
        # not a path: wrap an iterator on location which should be a sequence
        # of lines
        return iter(location)

    T = typecode.contenttype.get_type(location)

    if not T.is_file:
        return iter([])

    # Should we read this as some markup, pdf office doc, text or binary?
    if T.is_pdf:
        return unicode_text_lines_from_pdf(location)

#     if T.is_doc:
#         return unicode_text_lines_from_markup(location)

#     if markup.is_markup(location):
#         try:
#             new_loc = markup.convert_to_text(location)
#             return unicode_text_lines(new_loc)
#         except:
#             # try again with as plain text
#             pass

    if T.is_text:
        return unicode_text_lines(location)

    if T.is_binary:
        # fall back to binary
        return unicode_text_lines_from_binary(location)

    else:
        # if neither text, text-like nor binary: return empty
        # this should never happen
        return iter([])


def unicode_text_lines_from_binary(location):
    """
    Return an iterable over unicode text lines extracted from a binary file at
    location.
    """
    for line in strings.strings_in_file(location, filt=strings.filter_strict):
        yield line


def unicode_text_lines_from_pdf(location):
    """
    Return an iterable over unicode text lines extracted from a pdf file at
    location.
    """
    for line in pdf.get_text_lines(location):
        yield as_unicode(line)


def as_unicode(line):
    """
    Return a unicode text line from soem text line
    Try to decode line as Unicode. Try first some default encodings, 
    then attempt Unicode trans-literation and finally
    fall-back to ASCII strings extraction.

    TODO: Add file/magic detection, unicodedmanit/BS3/4 an chardet
    """
    try:
        s = unicode(line, 'utf-8')
    except UnicodeDecodeError:
        try:
            # FIXME: latin-1 may never fail
            s = unicode(line, 'latin-1')
        except UnicodeDecodeError:
            try:
                # Convert some byte string to ASCII characters as
                # Unicode including replacing accented characters with
                # their non- accented NFKD equivalent. Non ISO-Latin
                # and non ASCII characters are stripped from the
                # output. Does not preserve the original length
                # offsets. For Unicode NFKD equivalence, see:
                # http://en.wikipedia.org/wiki/Unicode_equivalence
                s = unicodedata.normalize('NFKD', line).encode('ASCII')
            except UnicodeDecodeError:
                try:
                    import chardet
                    enc = chardet.detect(line)['encoding']
                    s = unicode(line, enc)
                except UnicodeDecodeError:
                    # fall-back to strings extraction if all else
                    # fails
                    s = unicode(strings.remove_non_printable(s))
    return s


def unicode_text_lines(location):
    """
    Return an iterable over unicode text lines from a text file at location.
    Open the file as binary then try to decode each line as Unicode. Try first
    some default encodings, then attempt Unicode trans-literation and finally
    fall-back to ASCII strings extraction.

    TODO: Add file/magic detection, unicodedmanit/BS3/4 an chardet
    """
    with open(location, 'rbU') as f:
        for line in f:
            yield as_unicode(line)


def unicode_text(location):
    """
    Return a string guaranteed to be unicode from the content of the file at
    location. The whole file content is returned at once, which may be a
    problem for very large files.
    """
    return u''.join(unicode_text_lines(location))
