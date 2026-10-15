"""What a read must return: heal.locator(..., expect_value=...) and
mendwright.open(expect_values={...}).

A heal can pass every check of the gate and still be the wrong element. After a redesign, a
price selector that heals onto the rating next to the price reads "4.5", and a scraper
saves that. expect_value= says what a read of the key must look like. When a read goes
through a selector that a heal saved, mendwright checks the value it returns: one that
passes confirms a heal made in this process, as expect= on an action does, and one that
fails quarantines the heal and raises AttentionRequired instead of returning the value. A
read through the selector in your code is never checked, so a heal is the only thing this
can block.

The checks (expectation()): one of NAMES, a compiled regular expression (re.search) or a
callable that takes the value as a str and returns something truthy when it is right.

parse_number() and parse_money() read numbers as most locales write them: 1,299.00,
1.299,00, 1 299,00 and 1'299.00, with an Indian 1,29,999.00 too. A single . or , followed by
exactly three digits, as in 1.299 or 1,299, is read as a thousands separator, as a price
usually is; 0,299 and 1234.567 have a decimal part. In an amount of a currency with three
decimals (KWD, BHD, OMR, JOD, TND), that . is the decimal point, as those amounts are
written (KWD 1.250 is 1.250), and that , could be either (1,250 TND is 1.250 in Tunisia and
1250 in English), so parse_money() gives None for it, though it is money. The text is read
in its compatibility form first, so a non-breaking space is a space and a fullwidth digit a
digit, and digits of other scripts count as digits.
"""

from __future__ import annotations

import calendar
import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable
from urllib.parse import urlsplit

# The checks expect_value= takes by name, and how a message says a value is not one.
NAMES = {
    'money': 'is not money',
    'number': 'is not a number',
    'integer': 'is not a whole number',
    'percent': 'is not a percentage',
    'date': 'is not a date',
    'email': 'is not an email address',
    'url': 'is not a URL',
}

# The reads whose value is checked: Playwright's Locator reads, and Selenium's
# WebElement.text, get_attribute() and get_dom_attribute().
READS = frozenset(('inner_text', 'text_content', 'input_value', 'get_attribute', 'text',
                   'get_dom_attribute'))

VALUE_LIMIT = 80   # how much of a value a message quotes

# --- numbers -------------------------------------------------------------------------------

# Characters that group the digits of a number. ' ' also stands for a non-breaking, narrow or
# thin space, which the compatibility form turns into a space.
_GROUPERS = "'’ "
_BODY = re.compile(r"([+-]?) ?(\d(?:[\d.,'’ ]*\d)?)")


def _clean(text):
    """text in its compatibility form (NFKC), with the decimal digits of any script as ASCII
    digits, a minus sign as -, the Arabic separators and percent sign as , . and %, and runs
    of whitespace as one space, trimmed."""
    text = unicodedata.normalize('NFKC', text)
    out = []
    for ch in text:
        if ch.isdecimal():
            out.append(str(unicodedata.decimal(ch)))
        elif ch == '−':
            out.append('-')
        elif ch == '٬':
            out.append(',')
        elif ch == '٫':
            out.append('.')
        elif ch == '٪':
            out.append('%')
        else:
            out.append(ch)
    return ' '.join(''.join(out).split())


def _grouped(groups):
    """True when the digit groups of a number's whole part are grouped as numbers are: the
    first one 1 to 3 digits and each other one 3 (1,299,000), or the last one 3 and the ones
    before it 2 (an Indian 1,29,999)."""
    if not all(g.isdigit() for g in groups) or not 1 <= len(groups[0]) <= 3:
        return False
    if all(len(g) == 3 for g in groups[1:]):
        return True
    return (len(groups[0]) <= 2 and len(groups[-1]) == 3
            and all(len(g) == 2 for g in groups[1:-1]))


def _number(text, thousandths=False):
    """(Decimal, has a decimal part) for a cleaned text that is a number and nothing else,
    such as -1.299,50, else None. thousandths is for an amount of a currency with three
    decimals: a single . before exactly three digits is its decimal point, and a single ,
    there could be the point or a thousands separator, which gives (None, True)."""
    match = _BODY.fullmatch(text)
    if not match:
        return None
    sign, body = match.groups()
    unsure = False
    spaced = any(ch in _GROUPERS for ch in body)
    dots, commas = body.count('.'), body.count(',')
    if dots and commas:
        point = '.' if body.rfind('.') > body.rfind(',') else ','
        if body.count(point) != 1:
            return None
    elif dots or commas:
        mark = '.' if dots else ','
        whole, _, tail = body.rpartition(mark)
        if body.count(mark) > 1:
            point = None         # 1.299.000: it groups
        elif spaced:
            point = mark         # 1 299,50: the spaces group, so the mark is the point
        elif len(tail) == 3 and 1 <= len(whole) <= 3 and not whole.startswith('0'):
            point = None         # 1,299 and 1.299 read as thousands, as prices do
            if thousandths and mark == '.':
                point = mark     # KWD 1.250: one dinar and 250 fils
            elif thousandths:
                unsure = True    # 1,250 TND: 1.250 in Tunisia, 1250 in English
        else:
            point = mark
    else:
        point = None
    whole, frac = body.rpartition(point)[::2] if point else (body, '')
    if point and not frac.isdigit():
        return None
    if len({"'" if ch == '’' else ch for ch in whole if not ch.isdigit()}) > 1:
        return None              # two kinds of separators in the whole part: 1 299.000
    groups = re.split(r"[.,'’ ]", whole)
    if len(groups) > 1 and not _grouped(groups):
        return None
    digits = ''.join(groups)
    if unsure:
        return None, True
    return Decimal(sign + digits + ('.' + frac if frac else '')), bool(point)


def parse_number(text: Any) -> Decimal | None:
    """The number ``text`` is, as a Decimal, or None when it is not a number and nothing
    else: parse_number('1.299,00') and parse_number('1,299.00') are Decimal('1299.00'),
    parse_number('1 299,5') is Decimal('1299.5'), and '12 kg' or '$5' is None (see
    parse_money())."""
    if not isinstance(text, str):
        return None
    found = _number(_clean(text))
    return found[0] if found else None


# ISO 4217 codes and the local abbreviations prices are written with, in lower case.
_CODES = frozenset((
    'usd', 'eur', 'gbp', 'jpy', 'cny', 'rmb', 'chf', 'cad', 'aud', 'nzd', 'sek', 'nok', 'dkk',
    'isk', 'pln', 'czk', 'huf', 'ron', 'bgn', 'rsd', 'uah', 'rub', 'try', 'ils', 'inr', 'pkr',
    'bdt', 'lkr', 'npr', 'idr', 'myr', 'sgd', 'thb', 'vnd', 'php', 'krw', 'twd', 'hkd', 'brl',
    'mxn', 'ars', 'clp', 'cop', 'pen', 'uyu', 'zar', 'ngn', 'kes', 'ghs', 'egp', 'mad', 'tnd',
    'aed', 'sar', 'qar', 'kwd', 'bhd', 'omr', 'jod',
    'kr', 'kn', 'zł', 'zl', 'kč', 'kc', 'ft', 'lei', 'лв', 'руб', 'р', 'rs', 'r', 'rm', 'rp',
    'fr', 'sfr', 'tl', 'dh', 'dhs', 'грн', 'din', 'дин', 'ден',
))
# The codes of _CODES whose currency has three decimals: KWD 1.250 is one dinar 250 fils.
_THOUSANDTHS = frozenset(('kwd', 'bhd', 'omr', 'jod', 'tnd'))
_DOLLAR_PREFIX = re.compile(r'([A-Z]{1,3})\$')        # US$, C$, A$, R$, NZ$, HK$
_WORD_PREFIX = re.compile(r'([^\W\d_]{1,4})\.? ?(?=[\d+-])')
_WORD_SUFFIX = re.compile(r'(?<=[\d\-–—]) ?([^\W\d_]{1,4})\.?$')
# 129,- or 129:- or 12.– : a whole amount, written without its cents.
_NO_CENTS = re.compile(r'(?<=\d)[.,:][-–—]$')


def _symbol(ch):
    return unicodedata.category(ch) == 'Sc'


def _prefix(text):
    """(currency, rest) when text starts with a currency sign or code, else (None, text)."""
    if text and _symbol(text[0]):
        return text[0], text[1:].lstrip()
    match = _DOLLAR_PREFIX.match(text)
    if match:
        return match.group(0), text[match.end():].lstrip()
    match = _WORD_PREFIX.match(text)
    if match and match.group(1).casefold() in _CODES:
        return match.group(1), text[match.end():]
    return None, text


def _suffix(text, symbols=True):
    """(currency, rest) when text ends with a currency sign (with symbols) or code, else
    (None, text)."""
    if symbols and text and _symbol(text[-1]):
        return text[-1], text[:-1].rstrip()
    match = _WORD_SUFFIX.search(text)
    if match and match.group(1).casefold() in _CODES:
        return match.group(1), text[:match.start()].rstrip()
    return None, text


def _money(text):
    """(Decimal, currency) for a cleaned text that is an amount with a currency sign or code
    before or after it, or written as 129,- or 129:-, else None. A sign may come before the
    currency or the number. The Decimal is None for an amount that is money but could be
    read two ways (see _number's thousandths)."""
    sign = ''
    if text[:1] in ('-', '+') and text[1:2] and not text[1:2].isdigit():
        sign, text = text[0], text[1:].lstrip()
    currency, rest = _prefix(text)
    if currency is not None:
        rest = _suffix(rest, symbols=False)[1]    # $12.00 USD
    else:
        currency, rest = _suffix(text)
    if _NO_CENTS.search(rest):
        currency = currency or rest[-2:]
        rest = rest[:-2]
    if currency is None or (sign and rest[:1] in ('-', '+')):
        return None
    found = _number(sign + rest, thousandths=currency.casefold() in _THOUSANDTHS)
    return (found[0], currency) if found else None


def parse_money(text: Any) -> Decimal | None:
    """The amount of a price, as a Decimal, or None when ``text`` is not an amount with a
    currency sign ($, €, £, ¥, ₹ and the other Unicode currency signs, or US$ and the like) or
    a currency code (USD, EUR, ...) or abbreviation (kr, zł, Kč, Ft, lei, Rs, R, CHF, ...)
    before or after it, and nothing else. parse_money('$1,299.00'), parse_money('1.299,00 €')
    and parse_money('USD 1299') are Decimal('1299.00') or Decimal('1299'), parse_money('129
    kr') is Decimal('129'), and '129,-' or '129:-' reads as 129 too. A minus sign may come
    before the currency or the amount. '1,299.00' without a currency is None (see
    parse_number()). In a currency with three decimals (KWD, BHD, OMR, JOD, TND), a single .
    before three digits is the decimal point, so parse_money('KWD 1.250') is
    Decimal('1.250'), and a single , there is None, since 'TND 1,250' is 1.250 in Tunisia
    and 1250 in English."""
    if not isinstance(text, str):
        return None
    found = _money(_clean(text))
    return found[0] if found else None


def _percent(text):
    """The number of a percentage such as 12.5 %, -15% or %20, else None."""
    if text.endswith('%'):
        rest = text[:-1].rstrip()
    elif text.startswith('%'):
        rest = text[1:].lstrip()
    else:
        return None
    found = _number(rest)
    return found[0] if found else None


# --- dates -----------------------------------------------------------------------------------

def _bare(word):
    """word in lower case without its accents: Août is aout, März is marz."""
    decomposed = unicodedata.normalize('NFKD', word.casefold())
    return ''.join(ch for ch in decomposed if not unicodedata.combining(ch))


# Month names and abbreviations in English, German, French, Spanish, Italian, Dutch and
# Portuguese, without accents.
_MONTH_WORDS = (
    'january jan januar janner janvier janv enero ene gennaio gen januari janeiro',
    'february feb februar fevrier fevr fev febrero febbraio februari fevereiro',
    'march mar marz mrz mars marzo maart mrt marco',
    'april apr avril avr abril abr aprile',
    'may mai mayo maggio mag mei maio',
    'june jun juni juin junio giugno giu junho',
    'july jul juli juillet juil julio luglio lug julho',
    'august aug aout agosto ago augustus',
    'september sep sept septembre septiembre setiembre set settembre setembro',
    'october oct oktober okt octobre octubre ottobre ott outubro out',
    'november nov novembre noviembre novembro',
    'december dec dezember dez decembre diciembre dic dicembre dezembro',
)
_MONTHS = {word: n for n, words in enumerate(_MONTH_WORDS, 1) for word in words.split()}
_WEEKDAYS = frozenset((
    'monday mon tuesday tue tues wednesday wed thursday thu thur thurs friday fri saturday '
    'sat sunday sun montag dienstag mittwoch donnerstag freitag samstag sonnabend sonntag mo '
    'di mi do fr sa so lundi mardi mercredi jeudi vendredi samedi dimanche lun mar mer jeu ven '
    'sam dim lunes martes miercoles jueves viernes sabado domingo mie jue vie sab dom lunedi '
    'martedi mercoledi giovedi venerdi domenica maandag dinsdag woensdag donderdag vrijdag '
    'zaterdag zondag ma wo vr za zo segunda terca quarta quinta sexta feira').split())
# Words a written date may have besides its parts: 15 de octubre de 2026, the 15th of
# October, 1er octobre, 1º de mayo (º reads as o in compatibility form).
_FILLERS = frozenset('de del of the le la am den el du st nd rd th er e o'.split())
_ISO = re.compile(r'(\d{4})-(\d{1,2})-(\d{1,2})(?:[t ]\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?'
                  r' ?(?:z|[+-]\d{2}:?\d{2})?)?')
_NUMERIC = re.compile(r'(\d{1,4})([./-])(\d{1,2})\2(\d{1,4})\.?')
_CJK = re.compile(r'(\d{4}) ?[年년] ?(\d{1,2}) ?[月월] ?(\d{1,2}) ?[日일]')
_TIME = re.compile(r',? ?(?:(?:at|um|à|a|às|as|alle|om) )?\d{1,2}(?::\d{2}){1,2}(?: ?[ap]\.?m\.?)?'
                   r'(?: uhr)?$|,? ?\d{1,2}h\d{2}$')
_PIECES = re.compile(r'\d+|[^\W\d_]+')


def _real(year, month, day):
    """True for a calendar date. year None is any leap year."""
    if not 1 <= month <= 12 or day < 1:
        return False
    return day <= calendar.monthrange(2000 if year is None else year, month)[1]


def _year(text):
    """A year from 2 or 4 digits (26 is 2026), else None."""
    if len(text) == 4:
        return int(text)
    return 2000 + int(text) if len(text) == 2 else None


def _numeric_date(a, b, c):
    """True when a sep b sep c is a date as Y-M-D, D-M-Y or M-D-Y."""
    if len(a) == 4:
        return _real(int(a), int(b), int(c))
    year = _year(c)
    if year is None or len(a) > 2:
        return False
    return _real(year, int(b), int(a)) or _real(year, int(a), int(b))


def _worded_date(pieces):
    """True when the pieces of a date written with its month's name are a day, the month
    and a year in an order dates are written in."""
    months = [i for i, p in enumerate(pieces) if p in _MONTHS]
    if len(months) > 1 and pieces[0] in _WEEKDAYS:
        pieces, months = pieces[1:], [i - 1 for i in months[1:]]
    if len(months) != 1:
        return False
    month = _MONTHS[pieces[months[0]]]
    rest = [p for i, p in enumerate(pieces) if i != months[0]]
    if not rest or not all(p.isdigit() for p in rest) or len(rest) > 2:
        return False
    at = months[0]
    if len(rest) == 1:
        value = rest[0]
        if len(value) == 4:
            return at == 0          # October 2026
        return len(value) <= 2 and _real(None, month, int(value))     # 15 Oct, Oct 15
    first, second = rest
    if at == 0 or (at == 1 and len(first) <= 2):     # Oct 15 2026, 15 Oct 2026
        year = _year(second)
        return len(first) <= 2 and year is not None and _real(year, month, int(first))
    if at == 1:                                       # 2026 Oct 15
        return len(first) == 4 and len(second) <= 2 and _real(int(first), month, int(second))
    return False


def _is_date(text):
    text = _TIME.sub('', _clean(text).casefold()).strip()
    if not text:
        return False
    match = _ISO.fullmatch(text)
    if match:
        return _real(*(int(g) for g in match.groups()))
    match = _NUMERIC.fullmatch(text)
    if match:
        a, _, b, c = match.groups()
        return _numeric_date(a, b, c)
    match = _CJK.fullmatch(text)
    if match:
        return _real(*(int(g) for g in match.groups()))
    pieces = [_bare(p) for p in _PIECES.findall(text)]
    pieces = [p for p in pieces if p not in _FILLERS
              and not (p in _WEEKDAYS and p not in _MONTHS)]
    return _worded_date(pieces)


# --- the other checks ------------------------------------------------------------------------

_EMAIL = re.compile(r'(?:mailto:)?[^\s@<>()\[\],;:"\']+@[^\s@<>()\[\],;:"\'.]+'
                    r'(?:\.[^\s@<>()\[\],;:"\'.?]+)+(?:\?\S*)?', re.IGNORECASE)


def _is_url(text):
    """An http or https URL with a host, one that starts with // (no scheme), or a path that
    starts with /, ./ or ../, as get_attribute('href') gives for a relative link. No spaces."""
    if not text or any(ch.isspace() for ch in text):
        return False
    if text.startswith(('./', '../')) or (text.startswith('/') and not text.startswith('//')):
        return True
    try:
        parts = urlsplit('https:' + text if text.startswith('//') else text)
        host = parts.hostname
    except ValueError:
        return False
    return parts.scheme.lower() in ('http', 'https') and bool(host)


def _is_integer(text):
    found = _number(_clean(text))
    return found is not None and not found[1]


_CHECKS = {
    'money': lambda text: _money(_clean(text)) is not None,
    'number': lambda text: _number(_clean(text)) is not None,
    'integer': _is_integer,
    'percent': lambda text: _percent(_clean(text)) is not None,
    'date': _is_date,
    'email': lambda text: _EMAIL.fullmatch(text.strip()) is not None,
    'url': lambda text: _is_url(text.strip()),
}


# --- expect_value= -------------------------------------------------------------------------

@dataclass(frozen=True)
class Expectation:
    """What expect_value= asked for. passes(value) says whether a read's value is that;
    shown is how a message names the argument, refusal how it says a value is not that."""
    spec: Any
    test: Callable[[str], Any]
    shown: str
    refusal: str

    def passes(self, value: Any) -> bool:
        """True when value is a str that the check accepts. A check that raises refuses."""
        if not isinstance(value, str):
            return False
        try:
            return bool(self.test(value))
        except Exception:
            return False


def expectation(spec: Any, key: Any = None) -> Expectation | None:
    """The Expectation for an expect_value= argument, or None for None. Raises ValueError
    for a name that is not one of NAMES, and TypeError for anything else that is not a
    compiled regular expression of str or a callable."""
    if spec is None or isinstance(spec, Expectation):
        return spec
    where = 'expect_value= for ' + repr(key) if key is not None else 'expect_value='
    if isinstance(spec, str):
        name = spec.strip().lower()
        if name not in NAMES:
            raise ValueError('mendwright: ' + where + ' takes ' + _listed(NAMES) + ', a compiled '
                             'regular expression or a callable, got ' + repr(spec) + '.')
        return Expectation(name, _CHECKS[name], 'expect_value=' + repr(name), NAMES[name])
    if isinstance(spec, re.Pattern):
        if not isinstance(spec.pattern, str):
            raise TypeError('mendwright: ' + where + ' got a bytes pattern; a read returns '
                            'text, so compile the pattern from a str.')
        return Expectation(spec, spec.search, 'expect_value=re.compile(' + repr(spec.pattern)
                           + ')', 'does not match ' + repr(spec.pattern))
    if callable(spec):
        name = getattr(spec, '__name__', '')
        named = name if name and name.isidentifier() else ''
        return Expectation(spec, spec, 'expect_value=' + (named or 'a callable'),
                           'was refused by ' + (named + '()' if named else 'the callable'))
    raise TypeError('mendwright: ' + where + ' takes ' + _listed(NAMES) + ', a compiled regular '
                    'expression or a callable that takes the value and returns True when it is '
                    'right, got ' + type(spec).__name__ + '.')


def expectations(specs: Any) -> dict:
    """{key: Expectation} for open(expect_values=...): a mapping of keys to what
    expect_value= takes. Raises TypeError or ValueError for anything else."""
    if specs is None:
        return {}
    if not hasattr(specs, 'items'):
        raise TypeError('mendwright: expect_values= takes a dict of keys to checks, such as '
                        "{'#price': 'money'}, got " + type(specs).__name__ + '.')
    out = {}
    for key, spec in specs.items():
        if not isinstance(key, str) or not key:
            raise TypeError('mendwright: expect_values= takes keys as strings, got '
                            + repr(key) + '.')
        found = expectation(spec, key)
        if found is not None:
            out[key] = found
    return out


def _listed(names):
    names = ["'" + n + "'" for n in names]
    return ', '.join(names[:-1]) + ' or ' + names[-1]


def read_text(verb, args):
    """How a message names a read: inner_text(), get_attribute('content'), text."""
    if verb == 'text':
        return 'text'
    return verb + '(' + ', '.join(repr(a) for a in args) + ')'


def quoted(value):
    """A value as a message quotes it, cut to VALUE_LIMIT characters."""
    text = repr(value)
    return text if len(text) <= VALUE_LIMIT else text[:VALUE_LIMIT - 3] + '...'


def refusal_reason(verb, args, expect):
    """The reason a journal line gives for a read that failed its check, without the value:
    "expect_value: inner_text() is not money"."""
    return 'expect_value: ' + read_text(verb, args) + ' ' + expect.refusal


def refused_message(key, xpath, verb, args, value, expect):
    """The text of the AttentionRequired (and the WARNING) for a read through a heal whose
    value failed its check."""
    return ("'" + key + "' was healed to " + xpath + ', and its ' + read_text(verb, args)
            + ' there is ' + quoted(value) + ', which ' + expect.refusal + ' (' + expect.shown
            + '). The heal probably found another element, so mendwright quarantined it and '
            "did not return the value. The next lookup of '" + key + "' heals it again, and "
            'cannot pick that element. If the value is right, change expect_value=.')
