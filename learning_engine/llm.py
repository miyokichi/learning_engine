"""Optional OpenAI-compatible chat endpoint (e.g. local Ollama or LM Studio).

No outbound request is made unless the user configures a model and URL.
LLM output is always a draft; the user must confirm before publishing knowledge.
"""
import json
import urllib.request
from urllib.parse import urlparse
import copy


def validate_procedure(value):
    if not isinstance(value, dict):
        raise ValueError("Procedure must be an object")
    result = copy.deepcopy(value)
    required = {"trigger", "goal", "steps", "outcome", "decision_criteria", "unverified_inferences", "missing"}
    if not required <= result.keys():
        raise ValueError("Missing procedure fields")
    if set(result) - required - {"observed_order", "required_order", "limitations", "scope", "validation_evidence"}:
        raise ValueError("Unsupported procedure field")
    for key in ("trigger", "goal", "outcome"):
        if result[key] is not None and not isinstance(result[key], str):
            raise ValueError(f"Invalid {key}")
    if not isinstance(result["steps"], list) or not result["steps"]:
        raise ValueError("Procedure needs at least one step")
    for step in result["steps"]:
        if not isinstance(step, dict) or not isinstance(step.get("action"), str) or not step["action"].strip():
            raise ValueError("Each step needs an action")
        for key in ("reason", "result"):
            if step.get(key) is not None and not isinstance(step[key], str):
                raise ValueError("Invalid step field")
    for key in ("decision_criteria", "unverified_inferences", "missing"):
        if not isinstance(result[key], list) or any(not isinstance(s, str) for s in result[key]):
            raise ValueError(f"Invalid {key}")
    if set(result["missing"]) - {"trigger", "reason", "result", "validation"}:
        raise ValueError("Invalid missing enum")
    result["observed_order"] = [s["action"] for s in result["steps"]]
    # No extractor can infer a mandatory dependency order from one report.
    result["required_order"] = "unknown"
    result.setdefault("limitations", [])
    if not isinstance(result["limitations"], list) or any(not isinstance(s, str) for s in result["limitations"]):
        raise ValueError("Invalid limitations")
    if "scope" in result and not isinstance(result["scope"], dict):
        raise ValueError("Invalid scope")
    return result


class FakeExtractor:
    """Offline fixed response. The supplied fixture is never expert approval."""
    def __init__(self, response, suggestions=None):
        self.response = response
        self.suggestions = suggestions

    def extract(self, text):
        return validate_procedure(self.response)

    def suggest(self, task, text, context):
        return validate_suggestion(self.suggestions)


def validate_suggestion(value):
    keys = {"interpretation", "questions", "hypotheses", "rule_candidates", "concepts"}
    if not isinstance(value, dict) or set(value) != keys or not isinstance(value["interpretation"], str):
        raise ValueError("Invalid suggestion object")
    for key in ("questions", "hypotheses", "concepts"):
        if not isinstance(value[key], list) or any(not isinstance(item, str) for item in value[key]):
            raise ValueError("Invalid suggestion list")
    if len(value["questions"]) > 2:
        raise ValueError("Question batch exceeds two")
    if not isinstance(value["rule_candidates"], list):
        raise ValueError("Invalid rule candidates")
    for rule in value["rule_candidates"]:
        if not isinstance(rule, dict) or set(rule) != {"conclusion", "predicates", "reason"}:
            raise ValueError("Invalid rule candidate")
        if not isinstance(rule["conclusion"], str) or not isinstance(rule["reason"], str):
            raise ValueError("Invalid rule candidate text")
        if not isinstance(rule["predicates"], list) or not rule["predicates"]:
            raise ValueError("Rule needs explicit predicates")
        for p in rule["predicates"]:
            from .knowledge import validate_feature
            if not isinstance(p, dict) or set(p) != {"feature", "op", "value"} or not isinstance(p["feature"], str) or p["op"] not in {"eq", "ne", "gt", "gte", "lt", "lte"}:
                raise ValueError("Invalid rule predicate")
            validate_feature({"value": p["value"], "status": "expert_confirmed"})
    return copy.deepcopy(value)


SUGGEST_SYSTEM = """Propose interpretations, questions, hypotheses, rule candidates and
new concepts from an expert conversation. These are UNVERIFIED suggestions only.
Use only the supplied evidence and canonical feature ids. Never invent numeric
thresholds from two examples. Do not ask about information already present in
the context. Ask at most two questions; explain in each question which decision
the answer changes. Return JSON with exactly these keys:
interpretation (string), questions (list of strings), hypotheses (list of strings),
concepts (list of strings), rule_candidates (list of objects with conclusion
(string), reason (string), predicates (list of {feature, op, value})).
Allowed op: eq, ne, lt, lte, gt, gte. Use Japanese. Do not claim expert approval."""

SYSTEM = """You extract draft procedural knowledge from an expert's first-person
natural-language report. Do not invent actions, evidence, causes, conditions, or
results. Keep uncertainty explicit. Return ONLY valid JSON in this shape:
{
  "trigger": "situation when procedure was useful, or null",
  "goal": "what they tried to achieve, or null",
  "steps": [{"action":"...","reason":"... or null","result":"... or null"}],
  "outcome": "overall result, or null",
  "decision_criteria": ["..."],
  "unverified_inferences": ["..."],
  "missing": ["trigger"|"reason"|"result"|"validation"]
}
Use a string null only as JSON null; omit no keys. Keep details in Japanese.
An order in the report does not prove a required step order. Report any inferred
step order as unverified. Never interpret silence as expert approval."""

class LocalChatExtractor:
    def __init__(self, url, model, api_key="local", timeout=60):
        if not isinstance(url, str) or not url.strip():
            raise ValueError("Set an explicit HTTP(S) OpenAI-compatible endpoint URL")
        url = url.strip()
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Endpoint URL must use http:// or https:// and include a hostname")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Invalid endpoint URL")
        if any(c.isspace() for c in url) or "\\" in url:
            raise ValueError("Invalid endpoint URL")
        try:
            port = parsed.port
        except ValueError:
            raise ValueError("Invalid endpoint port") from None
        if port == 0:
            raise ValueError("Invalid endpoint port")
        if not model:
            raise ValueError("Model is required")
        self.url, self.model, self.api_key, self.timeout = url.rstrip('/'), model, api_key, timeout

    def extract(self, text):
        return validate_procedure(self._complete(SYSTEM, text))

    def suggest(self, task, text, context):
        if task not in {"case", "answer", "correction", "concept"}:
            raise ValueError("Unknown suggestion task")
        return validate_suggestion(self._complete(SUGGEST_SYSTEM, json.dumps({"task": task, "text": text, "context": context}, ensure_ascii=False)))

    def _complete(self, system, text):
        request = urllib.request.Request(
            self.url + "/chat/completions",
            data=json.dumps({
                "model": self.model,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": text}],
                "temperature": 0,
                "response_format": {"type": "json_object"},
            }, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"},
        )
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                raise ValueError("LLM redirects are disabled")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open(request, timeout=self.timeout) as res:
            payload = json.load(res)
        content = payload["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        return parsed
