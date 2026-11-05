"""Spoken text preparation for ZipVoice's Chinese and English speakers."""
import re
import unicodedata


_WIDTH = str.maketrans({chr(code): chr(code - 0xFEE0) for code in
                       (*range(0xFF10, 0xFF1A), *range(0xFF21, 0xFF3B), *range(0xFF41, 0xFF5B), 0xFF0E, 0xFF05, 0xFF0B, 0xFF0D)})
_EMOJI = re.compile(r"(?:[#*0-9][\ufe0f\ufe0e]?\u20e3|[©®™↔-↙↩↪⬆-⬇]\ufe0f|[\U0001f000-\U0001faff\u2600-\u27bf\u200d\ufe0e\ufe0f\U000e0020-\U000e007f])+")
_TITLE = r"(?i:Mrs|Mr|Ms|Dr|Prof)\."
_EN_WORD = rf"(?:[A-Za-z]\.){{2,}}|{_TITLE}|[A-Za-z]+(?:['’\-][A-Za-z]+)*"
_TITLES = {'mrs.': 'missus', 'mr.': 'mister', 'ms.': 'miz', 'dr.': 'doctor', 'prof.': 'professor'}
_EN_PHRASE = re.compile(rf"(?:{_EN_WORD})(?:(?:[ \t]+|[、,][ \t]*)(?:{_EN_WORD}))*")
_ACRONYM = re.compile(r"(?:[A-Za-z]\.){2,}|[A-Z]{2,5}|[A-Za-z]")
_DATE = r"[0-9]{4}[-/][0-9]{1,2}[-/][0-9]{1,2}(?![0-9])"
_TIME = r"[0-9]{1,2}:[0-9]{2}(?::[0-9]{2})?(?![0-9])"
_VERSION = r"[0-9]+(?:\.[0-9]+){2,}"
_NUMBER = r"[+-]?(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]+)?%?"
_RANGE = rf"(?<![A-Za-z0-9]){_NUMBER}(?:[~～—–]|-(?={_NUMBER}\s*(?:件|台|个|天|小时|分钟|毫米|厘米|米|公斤|克|%))){_NUMBER}"
_NUMERIC_TOKEN = rf"{_DATE}|{_TIME}|{_VERSION}|{_RANGE}|{_NUMBER}"
_NUMBER_OR_WORD = re.compile(rf"{_NUMERIC_TOKEN}|(?:{_EN_WORD})")
_NUMERIC = re.compile(rf"{_DATE}|{_TIME}|{_VERSION}|(?P<range>{_RANGE})|{_NUMBER}")
_RANGE_VALUES = re.compile(rf'({_NUMBER})[~～—–-]({_NUMBER})')
_ORDER_PREFIX = re.compile(r"(?:工单(?:编号|号)?|单号|编号|工号|料号|批次号)[ \t：:#]*$")
_STATUS_AFTER_ID = re.compile(r"[ \t]+(?=已(?:完成|入库|开工|报工)|目前|正在|预计|剩余)")
_SENTENCE_END = re.compile(r"(?:[。！？；!?;\n]+|…+|(?<![A-Za-z])\.(?!\d)|(?<=[A-Za-z])\.(?![A-Za-z0-9]))[”’」』》）】\"']*")
_SOFT_END = re.compile(r"[，、,：:]|[ \t]+")
_DIGITS = str.maketrans('0123456789', '零一二三四五六七八九')


def spoken_text(text):
    # Width folding also lets the keycap pattern recognize full-width digits.
    text = text.translate(_WIDTH)
    def skip_emoji(match):
        before = text[match.start()-1:match.start()]
        after = text[match.end():match.end()+1]
        return ' ' if before.isascii() and before.isalnum() and after.isascii() and after.isalnum() else ''
    text = _EMOJI.sub(skip_emoji, text).replace('\r\n', '\n').replace('\r', '\n')
    text = re.sub(r'(?m)^\s*[-*•]\s+', '', text)
    text = re.sub(r'\*\*|__|`', '', text)
    return text.strip()


def has_speech(text):
    return any(unicodedata.category(char)[0] in 'LN' for char in text)


def breakpoints(text):
    tokens = list(_NUMBER_OR_WORD.finditer(text))
    protected = {offset for token in tokens for offset in range(token.start() + 1, token.end())}
    protected_dots = protected | {token.end() for token in tokens if re.fullmatch(rf'(?:[A-Za-z]\.){{2,}}|{_TITLE}', token[0])}
    return (
        [match.end() for match in _SENTENCE_END.finditer(text)
         if match.start() + 1 not in protected_dots],
        [match.end() for match in _SOFT_END.finditer(text) if match.end() not in protected],
        protected,
    )


def cardinal(digits):
    digits = digits.lstrip('0') or '0'
    if len(digits) > 16:
        return digits.translate(_DIGITS)
    groups = [digits[max(0, end - 4):end] for end in range(len(digits), 0, -4)]
    result = ''
    gap = False
    for group, unit in reversed(list(zip(groups, ('', '万', '亿', '兆')))):
        if not int(group):
            gap = True
            continue
        if result and (gap or int(group) < 1000):
            result += '零'
        gap = False
        within = ''
        for digit, place in zip(group, '千百十个'[-len(group):]):
            if digit == '0':
                if within and not within.endswith('零'):
                    within += '零'
            else:
                within += digit.translate(_DIGITS) + (place if place != '个' else '')
        result += within.rstrip('零') + unit
    return (result[1:] if result.startswith('一十') else result) or '零'


def chinese_numbers(text, source, start):
    def amount(token):
        sign = {'+': '正', '-': '负'}.get(token[0], '')
        percentage = token.endswith('%')
        integer, _, fraction = token.lstrip('+-').rstrip('%').replace(',', '').partition('.')
        return sign + ('百分之' if percentage else '') + cardinal(integer) + ('点' + fraction.translate(_DIGITS) if fraction else '')

    clause_offsets = set()
    def read_number(match):
        token = match[0]
        if re.fullmatch(_DATE, token):
            year, month, day = re.split(r'[-/]', token)
            if 1 <= int(month) <= 12 and 1 <= int(day) <= 31:
                return year.translate(_DIGITS) + '年' + cardinal(month) + '月' + cardinal(day) + '日'
            return '杠'.join(value.translate(_DIGITS) for value in (year, month, day))
        if re.fullmatch(_TIME, token):
            return ''.join(cardinal(value) + unit for value, unit in zip(token.split(':'), '点分秒'))
        if re.fullmatch(_VERSION, token):
            return '点'.join(value.translate(_DIGITS) for value in token.split('.'))
        if match.group('range'):
            left, right = _RANGE_VALUES.fullmatch(token).groups()
            if right.endswith('%') and not left.endswith('%'):
                left += '%'
            return amount(left) + '到' + amount(right)
        sign = ''
        offset = start + match.start()
        prefix = source[:offset].rstrip()
        if token.startswith(('+', '-')):
            sign = '正' if token[0] == '+' else '负'
            if token[0] == '-' and re.fullmatch(r'[A-Za-z]', source[offset-1:offset]):
                sign = '杠'
            token = token[1:]
        percentage = token.endswith('%')
        integer, _, fraction = token.rstrip('%').replace(',', '').partition('.')
        if not (percentage or fraction) and (sign == '杠' or integer.startswith('0') or _ORDER_PREFIX.search(prefix)):
            if _STATUS_AFTER_ID.match(text, match.end()):
                clause_offsets.add(match.end())
            return sign + integer.translate(_DIGITS)
        return amount(match[0])

    # Preserve a short spoken pause between an identifier and its status clause.
    parts = []
    previous = 0
    for match in _NUMERIC.finditer(text):
        parts.append(text[previous:match.start()])
        parts.append(read_number(match))
        previous = match.end()
        if previous in clause_offsets:
            parts.append('，')
            previous = _STATUS_AFTER_ID.match(text, previous).end()
    parts.append(text[previous:])
    return ''.join(parts)


def mandarin_text(text):
    numbered = chinese_numbers(text, text, 0)

    def spell(phrase):
        multiple_words = len(re.findall(_EN_WORD, phrase[0])) > 1
        def say(word):
            token = word[0]
            title = _TITLES.get(token.lower())
            if title:
                return title
            if _ACRONYM.fullmatch(token) and not (token == 'a' and multiple_words):
                # ZipVoice's en-US frontend reads uppercase A as /eɪ/.
                return ' '.join(token.upper().replace('.', ''))
            return token.lower() if token.isupper() else token
        before = numbered[phrase.start()-1:phrase.start()]
        after = numbered[phrase.end():phrase.end()+1]
        return (' ' if before.isalnum() else '') + re.sub(_EN_WORD, say, phrase[0]) + (' ' if after.isalnum() else '')

    return _EN_PHRASE.sub(spell, numbered).strip()
