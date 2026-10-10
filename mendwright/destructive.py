"""The destructive word check: the words that make mendwright treat a control as one that
pays, buys, submits or deletes, and the labels it reads them from.

The facade treats a key as destructive when one of these words is in the key, its
expected text, or the label of the button or link it matched. The gate (validation and
gate.py) uses the same words to refuse a heal onto an element whose label looks
destructive, unless the key was opted out with destructive=False.

This module imports nothing else from mendwright, so every layer can use it.
"""

import re

# Kept short on purpose; a false positive only means "fix this one by hand".
DESTRUCTIVE_WORDS = ('pay', 'payment', 'purchase', 'buy', 'checkout', 'confirm', 'submit',
                     'delete')
DESTRUCTIVE_PHRASES = ('place order', 'order now', 'complete order')

# <input type=...> values whose label is their value (or alt) attribute.
LABEL_INPUT_TYPES = ('submit', 'button', 'reset', 'image')
# Gate roles whose label the check reads. Other roles are left out: a field labelled
# "Confirm password" is not a destructive control.
LABELLED_ROLES = ('button', 'link')


def text_words(text):
    """'Pay now!' -> ['pay', 'now']"""
    return re.findall(r'[a-z0-9]+', str(text).lower())


def destructive_hit(words_or_text):
    """The destructive word or phrase in a word list (or a text), or None."""
    words = (text_words(words_or_text) if isinstance(words_or_text, str)
             else list(words_or_text))
    for word in DESTRUCTIVE_WORDS:
        if word in words:
            return word
    joined = ' ' + ' '.join(words) + ' '
    for phrase in DESTRUCTIVE_PHRASES:
        if ' ' + phrase + ' ' in joined:
            return phrase
    return None


def _norm(text):
    return ' '.join(str(text).split()) if isinstance(text, str) else ''


def _tag(el):
    return el.tag.lower() if isinstance(el.tag, str) else ''


def is_input_button(el):
    """True for <input type=submit|button|reset|image>, whose label is an attribute."""
    return (_tag(el) == 'input'
            and (el.get('type') or '').strip().lower() in LABEL_INPUT_TYPES)


def is_labelled_control(el, role):
    """True when the label check applies to el, whose gate role is ``role``: a button or
    link, or an input button."""
    return role in LABELLED_ROLES or is_input_button(el)


def element_labels(el, *, text=True):
    """Every label a person may read on el (an lxml element), each on its own: its
    visible text, its aria-label and title, the alt text of el and of images inside it,
    and the value of an input button. All are checked, because an aria-label replaces
    the text as the accessible name (<button aria-label="Delete account">x</button>).
    text=False leaves the visible text out. Empty labels are dropped."""
    found = [el.text_content()] if text else []
    found += [el.get('aria-label'), el.get('title'), el.get('alt')]
    found += [img.get('alt') for img in el.iter('img')]
    if is_input_button(el):
        found.append(el.get('value'))
    labels = [_norm(value) for value in found]
    return [label for label in dict.fromkeys(labels) if label]


def destructive_label(labels):
    """(label, word) for the first label that contains a destructive word, or None."""
    for label in labels:
        hit = destructive_hit(label)
        if hit:
            return label, hit
    return None


def target_reason(label, hit):
    """The reason the gate gives for refusing a heal onto a destructive-looking element.
    The re-prompt shows it to the model."""
    return ('the element looks destructive (its label ' + repr(label[:80]) + ' contains '
            + repr(hit) + '), so mendwright will not heal onto it; choose a different element')
