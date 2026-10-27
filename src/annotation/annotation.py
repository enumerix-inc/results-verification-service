"""
annotation.py - sidecar sample annotations for acceptance evaluation (ADR-026).

An annotation file (YAML, one per experiment_id) assigns each sample in an
experiment a group, a role and the expected counts per channel, using ordered
regular-expression rules over SampleName. It is loaded by the service at
verification time; the harness and the workbook (including GroupName) are
never touched.

Expected counts per channel:
  * an integer  - the channel has an assay and this many copies are expected
  * 0           - the channel has an assay but no template (no-template control)
  * null        - the channel has NO assay on this sample
These are never conflated.

Validation is strict: every sample must match exactly one rule. Samples that
match no rule, or more than one, are reported with the reason.
"""

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROLE_NTC = "ntc"
ROLE_STANDARD = "standard"
_PLACEHOLDER = re.compile(r"^\{(\w+)\}$")


class AnnotationError(Exception):
    """The annotation file is malformed, or samples do not match it."""


@dataclass(frozen=True)
class Rule:
    name: str
    pattern: re.Pattern
    group: str                      # template over capture groups, e.g. "FourPlex_{level}"
    expected: dict                  # channel -> int | None | "{capture}"


@dataclass(frozen=True)
class SampleAnnotation:
    sample_name: str
    rule: str
    group: str
    role: str
    expected: dict                  # channel -> int | None
    captures: dict                  # named capture groups from the rule's pattern


@dataclass
class Annotation:
    experiment_id: str
    annotation_version: int
    sha256: str
    channels: list
    levels: dict                    # "1k" -> 1000
    rules: list = field(default_factory=list)

    def match(self, sample_name: str) -> list:
        """Return every rule whose pattern matches sample_name (0, 1 or more)."""
        return [r for r in self.rules if r.pattern.fullmatch(sample_name)]

    def _build(self, sample_name: str, rule: Rule) -> SampleAnnotation:
        captures = rule.pattern.fullmatch(sample_name).groupdict()
        expected = {ch: self._resolve(rule, ch, v, captures, sample_name)
                    for ch, v in rule.expected.items()}
        assay = [v for v in expected.values() if v is not None]
        role = ROLE_NTC if assay and all(v == 0 for v in assay) else ROLE_STANDARD
        return SampleAnnotation(sample_name, rule.name,
                                rule.group.format(**captures), role,
                                expected, captures)

    def _resolve(self, rule, channel, value, captures, sample_name):
        if not (isinstance(value, str) and _PLACEHOLDER.match(value)):
            return value
        key = _PLACEHOLDER.match(value).group(1)
        raw = captures.get(key)
        if raw not in self.levels:
            raise AnnotationError(
                f"{sample_name}: rule '{rule.name}' captured {key}='{raw}' for "
                f"{channel}, which is not in the level map")
        return self.levels[raw]

    def annotate(self, sample_names) -> list:
        """Annotate every sample; raise AnnotationError listing ALL problems."""
        problems, out, seen = [], [], set()
        for name in sample_names:
            if name in seen:
                problems.append(f"{name}: duplicate sample name")
                continue
            seen.add(name)
            hits = self.match(name)
            if not hits:
                problems.append(f"{name}: matched no rule")
            elif len(hits) > 1:
                problems.append(f"{name}: matched {len(hits)} rules "
                                f"({', '.join(r.name for r in hits)})")
            else:
                try:
                    out.append(self._build(name, hits[0]))
                except AnnotationError as e:
                    problems.append(str(e))
        if problems:
            raise AnnotationError(
                f"{self.experiment_id} annotation v{self.annotation_version}: "
                f"{len(problems)} problem(s):\n  " + "\n  ".join(problems))
        return out


def _fail(path, msg):
    raise AnnotationError(f"{path}: {msg}")


def parse_annotation(text: str, source: str = "<annotation>") -> Annotation:
    """Parse and structurally validate annotation YAML text."""
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    doc = yaml.safe_load(text)
    if not isinstance(doc, dict):
        _fail(source, "top level must be a mapping")
    for key in ("experiment_id", "annotation_version", "channels", "levels", "rules"):
        if key not in doc:
            _fail(source, f"missing required key '{key}'")
    if not isinstance(doc["annotation_version"], int) or isinstance(doc["annotation_version"], bool):
        _fail(source, "annotation_version must be an integer")
    channels = doc["channels"]
    if not isinstance(channels, list) or not channels or len(set(channels)) != len(channels):
        _fail(source, "channels must be a non-empty list of unique names")

    levels = {}
    if not isinstance(doc["levels"], dict):
        _fail(source, "levels must be a mapping of label -> integer")
    for label, val in doc["levels"].items():
        if not isinstance(val, int) or isinstance(val, bool) or val < 0:
            _fail(source, f"level '{label}' must be a non-negative integer, got {val!r}")
        levels[str(label)] = val

    rules, names = [], set()
    if not isinstance(doc["rules"], list) or not doc["rules"]:
        _fail(source, "rules must be a non-empty list")
    for i, r in enumerate(doc["rules"]):
        for key in ("name", "pattern", "group", "expected"):
            if not isinstance(r, dict) or key not in r:
                _fail(source, f"rule #{i + 1} is missing '{key}'")
        if r["name"] in names:
            _fail(source, f"duplicate rule name '{r['name']}'")
        names.add(r["name"])
        try:
            pattern = re.compile(r["pattern"])
        except re.error as e:
            _fail(source, f"rule '{r['name']}' has an invalid pattern: {e}")
        exp = r["expected"]
        if not isinstance(exp, dict) or set(exp) != set(channels):
            _fail(source, f"rule '{r['name']}': expected must list exactly the "
                          f"channels {channels} (use null for no assay)")
        for ch, v in exp.items():
            if v is None:
                continue
            if isinstance(v, str):
                m = _PLACEHOLDER.match(v)
                if not m or m.group(1) not in pattern.groupindex:
                    _fail(source, f"rule '{r['name']}' {ch}: '{v}' is not a capture "
                                  f"group of its pattern")
            elif not isinstance(v, int) or isinstance(v, bool) or v < 0:
                _fail(source, f"rule '{r['name']}' {ch}: must be null, a "
                              f"non-negative integer, or '{{capture}}', got {v!r}")
        rules.append(Rule(r["name"], pattern, r["group"], exp))

    return Annotation(str(doc["experiment_id"]), doc["annotation_version"],
                      sha, list(channels), levels, rules)


def load_annotation(path) -> Annotation:
    """Load an annotation file; sha256 is computed over the file's bytes."""
    path = Path(path)
    raw = path.read_bytes()
    ann = parse_annotation(raw.decode("utf-8"), str(path))
    ann.sha256 = hashlib.sha256(raw).hexdigest()
    return ann


def find_annotation(annotations_dir, experiment_id: str) -> Path:
    """Locate <annotations_dir>/<experiment_id>.yaml and check it declares that id."""
    path = Path(annotations_dir) / f"{experiment_id}.yaml"
    if not path.is_file():
        raise FileNotFoundError(f"No annotation file for '{experiment_id}' at {path}")
    declared = load_annotation(path).experiment_id
    if declared != experiment_id:
        raise AnnotationError(f"{path}: declares experiment_id '{declared}', "
                              f"expected '{experiment_id}'")
    return path
