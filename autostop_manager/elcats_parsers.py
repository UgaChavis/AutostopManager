"""Pure parsers for public catalog HTML; network permission belongs to the transport.

Only published links and literal GET-form mappings are interpreted. JavaScript is
never executed, identifiers never decoded, price URLs never used as OEM numbers.
Registry coverage does not imply that a provider permits automated access.
"""

from __future__ import annotations

import ast
import hashlib
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

_VOID = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "wbr"}
)
_CATALOG_HOSTS = frozenset({"elcats.ru", "www.elcats.ru", "japancats.ru", "www.japancats.ru", "ssangyong.exist.ru"})
_BLOCKED_PARAMETER = re.compile(r"(?:vin|frame|password|token|session|viewstate|eventvalidation)", re.I)
_BLOCKED_PATH = re.compile(r"(?:login|auth|register|account|price|private|api|\.axd|\.ashx)", re.I)
_JS_CALL = re.compile(r"^(?:javascript:\s*)?([A-Za-z_$][\w$]*)\s*\((.*)\)\s*;?\s*$", re.S)
_FUNCTION = re.compile(r"\bfunction\s+([A-Za-z_$][\w$]*)\s*\(([^)]*)\)\s*\{", re.S)
_ACTION = re.compile(r"(?:\.action\s*=\s*|\.attr\s*\(\s*['\"]action['\"]\s*,\s*)(['\"])([^'\"]+)\1", re.I)
_ASSIGNMENT = re.compile(r"document\.forms\s*\[\s*\d+\s*\]\.(\w+)\.value\s*=\s*([^;]+);", re.I)
_PART_NUMBER = re.compile(r"(?=.*\d)[A-Za-z0-9][A-Za-z0-9 ._/-]{2,47}\Z")
_PR = re.compile(r"\bPR\s*:\s*([A-Z0-9]{3}(?:\s*[,/]\s*[A-Z0-9]{3})*)(?![A-Z0-9])", re.I)
_RESTRICTION = re.compile(
    r"\bPR\s*:|\b\d{2}[./]\d{2}(?:[./]\d{2,4})?\b|\b(?:19|20)\d{2}\s*[-–]|"
    r"не\s+для|not\s+for|\b(?:LHD|RHD|ABS|ESP|[A-Z]\d{2}[A-Z]?|[46]-?цилинд)\b",
    re.I,
)


@dataclass(eq=False)
class _Node:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    parent: _Node | None = None
    children: list[_Node | str] = field(default_factory=list)

    def text(self, *, scripts: bool = False) -> str:
        out: list[str] = []
        pending: list[_Node | str] = [self]
        while pending:
            item = pending.pop()
            if isinstance(item, str):
                out.append(item)
            elif scripts or item.tag not in {"script", "style"}:
                if item.tag == "br":
                    out.append(" ")
                pending.extend(reversed(item.children))
        return _space(" ".join(out))


class _Tree(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("document")
        self.stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"td", "th"} and self.stack[-1].tag in {"td", "th"}:
            self.stack.pop()
        if tag in {"tr", "li", "option"} and self.stack[-1].tag == tag:
            self.stack.pop()
        node = _Node(tag, {k: v or "" for k, v in attrs}, self.stack[-1])
        self.stack[-1].children.append(node)
        if tag not in _VOID and len(self.stack) < 128:
            self.stack.append(node)

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data: str) -> None:
        self.stack[-1].children.append(data)


def _space(value: str) -> str:
    return " ".join(value.replace("\xa0", " ").split())


def _nodes(node: _Node, tag: str | None = None) -> list[_Node]:
    out: list[_Node] = []
    pending = [node]
    while pending:
        item = pending.pop()
        if tag is None or item.tag == tag:
            out.append(item)
        pending.extend(reversed([c for c in item.children if isinstance(c, _Node)]))
    return out


def _closest(node: _Node, tag: str) -> _Node | None:
    current = node.parent
    while current is not None:
        if current.tag == tag:
            return current
        current = current.parent
    return None


def _cells(row: _Node) -> list[_Node]:
    return [c for c in row.children if isinstance(c, _Node) and c.tag in {"td", "th"}]


def _literal(value: str) -> str | None:
    value = value.strip()
    if value.startswith(("'", '"')):
        try:
            parsed = ast.literal_eval(value)
        except (ValueError, SyntaxError):
            return None
        return parsed if isinstance(parsed, str) else None
    if re.fullmatch(r"\d+|true|false", value, re.I):
        return value
    return None


def _arguments(value: str) -> list[str] | None:
    """Split only literal JS arguments; reject concatenation and executable expressions."""
    parts = re.findall(r"\s*('(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"|\d+|true|false)\s*(?:,|$)", value)
    if not parts:
        return [] if not value.strip() else None
    consumed = re.sub(r"\s*('(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"|\d+|true|false)\s*(?:,|$)", "", value)
    if consumed.strip():
        return None
    parsed = [_literal(p) for p in parts]
    return [str(p) for p in parsed] if all(p is not None for p in parsed) else None


def _function_body(script: str, start: int) -> str:
    depth, quote, escaped = 1, "", False
    for index in range(start, min(len(script), start + 30000)):
        char = script[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
        elif char in {"'", '"'}:
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if not depth:
                return script[start:index]
    return ""


def _functions(root: _Node) -> dict[str, tuple[list[str], str]]:
    functions: dict[str, tuple[list[str], str]] = {}
    for node in _nodes(root, "script"):
        script = "".join(c for c in node.children if isinstance(c, str))
        for match in _FUNCTION.finditer(script):
            params = [s.strip() for s in match.group(2).split(",") if s.strip()]
            functions[match.group(1)] = (params, _function_body(script, match.end()))
    return functions


def _safe_url(base: str, target: str, params: dict[str, str] | None = None, *, image: bool = False) -> str | None:
    try:
        result = urlsplit(urljoin(base, target))
        origin = urlsplit(base)
        port = result.port
    except ValueError:
        return None
    if result.scheme != "https" or result.hostname not in _CATALOG_HOSTS or result.hostname != origin.hostname:
        return None
    if result.username or result.password or port not in {None, 443}:
        return None
    if image:
        if not (result.path.lower().endswith("pcode.ashx") or re.search(r"\.(?:png|jpg|jpeg|gif)$", result.path, re.I)):
            return None
    elif not result.path.lower().endswith(".aspx") or _BLOCKED_PATH.search(result.path):
        return None
    query = dict(parse_qsl(result.query, keep_blank_values=True))
    query.update(params or {})
    if not image and any(_BLOCKED_PARAMETER.search(key) for key in query):
        return None
    return urlunsplit((result.scheme, result.netloc, result.path, urlencode(query), ""))


def _mapped_link(
    call: str,
    *,
    url: str,
    root: _Node,
    functions: dict[str, tuple[list[str], str]],
) -> tuple[str, dict[str, str]] | None:
    match = _JS_CALL.fullmatch(call.strip())
    if not match:
        return None
    values = _arguments(match.group(2))
    if values is None:
        return None
    if match.group(1) not in functions:
        return _japancats_link(values, url=url, root=root) if match.group(1) == "submit" else None
    names, body = functions[match.group(1)]
    action = _ACTION.search(body)
    if not action or not _public_get_form(root, body):
        return None
    bindings = dict(zip(names, values, strict=False))
    params: dict[str, str] = {}
    for assignment in _ASSIGNMENT.finditer(body):
        expression = assignment.group(2).strip()
        value = bindings.get(expression, _literal(expression))
        if value is None:
            return None
        params[assignment.group(1)] = value.strip()
    if not params:
        return None
    target = _safe_url(url, action.group(2), params)
    return (target, params) if target else None


def _public_get_form(root: _Node, body: str) -> bool:
    form_id = re.search(r"getElementById\s*\(\s*['\"]([^'\"]+)['\"]\s*\)", body)
    forms = _nodes(root, "form")
    if form_id:
        forms = [n for n in forms if n.attrs.get("id") == form_id.group(1)]
    return any(n.attrs.get("method", "get").lower() == "get" for n in forms)


def _japancats_link(values: list[str], *, url: str, root: _Node) -> tuple[str, dict[str, str]] | None:
    # Published CTreeViewScript template: submit(container_id, node_id, semicolon_values, needAuth).
    if len(values) != 4:
        return None
    containers = [n for n in _nodes(root) if n.attrs.get("id") == values[0]]
    if not containers:
        return None
    container = containers[0]
    controls = [n for n in _nodes(container, "input") if "readonly" not in n.attrs and n.attrs.get("name")]
    parameters = values[2].split(";")
    if len(parameters) < len(controls):
        return None
    params = {node.attrs["name"]: value for node, value in zip(controls, parameters, strict=False)}
    target = _safe_url(url, container.attrs.get("name", ""), params)
    return (target, params) if target else None


def _kind(url: str) -> str:
    route = urlsplit(url).path.rsplit("/", 1)[-1].lower()
    if route.startswith("parts"):
        return "parts"
    if route.startswith(("subgroup", "unit", "scheme", "illustration")):
        return "diagrams"
    if route.startswith(("groups", "group")):
        return "groups"
    if route.startswith(("modification", "options", "mdlyear", "models", "frames")):
        return "selections"
    return "models"


def _entity(target: str, current_kind: str) -> str:
    next_kind = _kind(target)
    if next_kind == "parts":
        return "diagram"
    if next_kind == "diagrams":
        return "group"
    if next_kind == "groups":
        return "modification" if current_kind in {"models", "selections"} else "group"
    return "selection"


def _native_id(params: dict[str, str], target: str) -> str:
    for key in ("SubId", "Unit", "GroupId", "Mdl", "Model", "Key", "Code", "Type"):
        if key in params:
            # A subgroup is only unique inside its catalog model.
            if key in {"GroupId", "Model"} and ("Group" in params or "SubGroup" in params):
                return ";".join(f"{k}={v}" for k, v in params.items() if k != "Title")
            dimensions = [k for k in ("Kpp", "Reg", "Year", "Market", "Region") if k in params]
            if dimensions:
                return ";".join(f"{k}={params[k]}" for k in [key, *dimensions])
            return params[key]
    return hashlib.sha256(target.encode()).hexdigest()[:24]


def _conditions(raw: str) -> tuple[dict[str, Any], list[str]]:
    raw = _space(raw)
    conditions: dict[str, Any] = {}
    unparsed: list[str] = []
    matches = list(_PR.finditer(raw))
    if matches:
        # A single listed PR clause is a list of alternatives. Multiple clauses
        # or additional boolean grammar stay unresolved rather than guessed.
        if (
            len(matches) == 1
            and not re.search(r"\b(?:AND|OR|NOT)\b|[+&!<>]|не\s*PR|исключ|кроме|без\s+PR", raw, re.I)
            and not re.match(r"\s*[-–]", raw[matches[0].end() :])
        ):
            codes = re.split(r"\s*[,/]\s*", matches[0].group(1).upper())
            conditions["pr_codes"] = {
                "all_of": codes if len(codes) == 1 else [],
                "any_of": codes if len(codes) > 1 else [],
                "none_of": [],
            }
        else:
            unparsed.append(raw)
    residue = _PR.sub("", raw)
    if _RESTRICTION.search(residue) and raw not in unparsed:
        unparsed.append(raw)
    return conditions, unparsed


def _placement(value: str) -> dict[str, str]:
    result: dict[str, str] = {}
    front = bool(re.search(r"передн|\b(?:front|frt)\b", value, re.I))
    rear = bool(re.search(r"задн|\b(?:rear|rr)\b", value, re.I))
    left = bool(re.search(r"\bлев\.?|\b(?:left|LH)\b", value, re.I))
    right = bool(re.search(r"\bправ\.?|\b(?:right|RH)\b", value, re.I))
    if front and not rear:
        result["axle"] = "front"
    elif rear and not front:
        result["axle"] = "rear"
    if left and not right:
        result["side"] = "left"
    elif right and not left:
        result["side"] = "right"
    return result


def _row_restrictions(row: dict[str, Any], raw: str) -> None:
    conditions, unparsed = _conditions(raw)
    row.update(
        conditions=conditions, raw_conditions=raw, unparsed_conditions=unparsed, unparsed_restrictions=unparsed.copy()
    )
    row.update(_placement(raw))


def _navigation_rows(root: _Node, *, url: str, page_kind: str) -> tuple[list[dict[str, Any]], list[str]]:
    rows: list[dict[str, Any]] = []
    warnings: list[str] = []
    functions = _functions(root)
    seen: set[str] = set()
    for anchor in _nodes(root, "a"):
        href = anchor.attrs.get("href", "")
        if href.lower().startswith("javascript:"):
            mapped = _mapped_link(href, url=url, root=root, functions=functions)
        else:
            target = _safe_url(url, href) if href and not href.startswith("#") else None
            mapped = (target, dict(parse_qsl(urlsplit(target).query))) if target else None
        if not mapped:
            continue
        target, params = mapped
        if target in seen or not params:
            continue
        seen.add(target)
        label = anchor.text() or anchor.attrs.get("title", "")
        if not label:
            label = next((n.attrs.get("title") or n.attrs.get("alt", "") for n in _nodes(anchor, "img")), "")
        if not label:
            label = params.get("Title", "")
        if not label:
            warnings.append("published_navigation_without_label")
            continue
        context = _closest(anchor, "tr")
        context_text = context.text() if context and len(context.text()) < 500 else label
        row: dict[str, Any] = {
            "id": _native_id(params, target),
            "label": label,
            "name": label,
            "url": target,
            "ref_params": params,
            "entity_kind": _entity(target, page_kind),
            "page_kind": _kind(target),
            "notes": context_text if context_text != label else "",
        }
        _row_restrictions(row, context_text)
        _model_context(row, anchor, page_kind=page_kind)
        if not _commercial_selection(params, label):
            rows.append(row)
        else:
            warnings.append("commercial_selection_excluded")
    return rows, list(dict.fromkeys(warnings))


def _model_context(row: dict[str, Any], anchor: _Node, *, page_kind: str) -> None:
    label = row["label"]
    if page_kind not in {"models", "selections"}:
        return
    context: dict[str, Any] = {}
    if re.fullmatch(r"(?:19|20)\d{2}", label):
        context["model_year"] = int(label)
        for parent in (anchor.parent, anchor.parent.parent if anchor.parent else None):
            if parent:
                headings = [n.text() for n in _nodes(parent) if n.tag in {"h2", "h3", "strong", "b"}]
                if headings:
                    context["model"] = headings[0]
                    row["label"] = row["name"] = f"{headings[0]} {label}"
                    break
    else:
        context["model"] = row["ref_params"].get("Title", label)
    if context:
        row["vehicle_context"] = context
        row.update(context)


def _commercial_selection(params: dict[str, str], label: str) -> bool:
    if params.get("Type") in {"3", "4", "5"}:
        return True
    return bool(re.search(r"грузов|автобус|коммерческ|commercial|motorcycle|мотоцикл", label, re.I))


def _subgroup_rows(root: _Node, *, url: str) -> list[dict[str, Any]]:
    if not urlsplit(url).path.lower().endswith("subgroup.aspx"):
        return []
    scripts = " ".join(n.text(scripts=True) for n in _nodes(root, "script"))
    if not re.search(r"['\"]Parts\.aspx['\"]", scripts, re.I):
        return []
    if not _public_get_form(root, scripts):
        return []
    query = dict(parse_qsl(urlsplit(url).query))
    model = query.get("Mdl") or query.get("Model")
    if not model:
        match = re.search(r"name=['\"]Mdl['\"]\s+value=['\"]([^'\"]+)['\"]", scripts)
        model = match.group(1) if match else None
    if not model:
        return []
    rows: list[dict[str, Any]] = []
    for node in _nodes(root, "tr"):
        cells = _cells(node)
        native_id = node.attrs.get("id", "")
        if not native_id or len(cells) < 4 or not re.fullmatch(r"[A-Za-z0-9_-]+", native_id):
            continue
        params = {"Mdl": model, "SubId": native_id}
        target = _safe_url(url, "Parts.aspx", params)
        if not target:
            continue
        name = cells[3].text()
        notes = " ".join(c.text() for c in cells[4:])
        row = {
            "id": native_id,
            "label": name,
            "name": name,
            "entity_kind": "diagram",
            "url": target,
            "ref_params": params,
            "diagram_number": cells[2].text(),
            "notes": notes,
        }
        _row_restrictions(row, notes)
        rows.append(row)
    return rows


def _header(row: _Node) -> dict[str, int]:
    headers: dict[str, int] = {}
    for index, cell in enumerate(_cells(row)):
        text = cell.text().lower()
        if re.search(r"код детали|код изделия|номер детали|part\s*(?:number|no)|каталожный номер", text):
            headers["number"] = index
        elif re.search(r"^поз|^позиция|^position|^callout", text):
            headers["position"] = index
        elif re.search(r"наименование|description|part name", text):
            headers["name"] = index
        elif re.search(r"кол-во|количество|quantity|^qty", text):
            headers["quantity"] = index
            if cell.attrs.get("colspan", "1") != "1":
                headers["quantity_columns"] = int(cell.attrs["colspan"]) if cell.attrs["colspan"].isdigit() else 0
        elif re.search(r"данные по модели|опции|application", text):
            headers["conditions"] = index
        elif re.search(r"примечание|доп. информация|remarks|notes", text):
            headers["notes"] = index
        elif "дата выпуска" in text:
            headers["production"] = index
        elif "тип замены" in text:
            headers["variant"] = index
        elif "тип трансмиссии" in text:
            headers["transmission"] = index
        elif text == "двигатель":
            headers["engine"] = index
    return headers


def _cell_text(cells: list[_Node], headers: dict[str, int], name: str) -> str:
    index = headers.get(name, len(cells))
    return cells[index].text() if index < len(cells) else ""


def _quantity(raw: str) -> int | float | None:
    if re.fullmatch(r"\d+(?:[.,]\d+)?", raw):
        value = float(raw.replace(",", "."))
        return int(value) if value.is_integer() else value
    return None


def _scheme_restrictions(root: _Node) -> str:
    nodes = [
        n
        for n in _nodes(root)
        if "divtitles" in n.attrs.get("id", "").lower() or n.attrs.get("id") == "schemeConditions" or n.tag == "caption"
    ]
    return _space(" ".join(n.text() for n in nodes))


def _number_image(cell: _Node, *, url: str) -> str | None:
    for image in _nodes(cell, "img"):
        target = _safe_url(url, image.attrs.get("src", ""), image=True)
        if target:
            return target
    return None


def _part_row(
    node: _Node,
    *,
    headers: dict[str, int],
    title: tuple[str, str],
    inherited: str,
    url: str,
    entry: dict[str, Any],
    quantities: list[str],
) -> dict[str, Any] | None:
    cells = _cells(node)
    if headers["number"] >= len(cells):
        return None
    raw_number = _cell_text(cells, headers, "number")
    image = _number_image(cells[headers["number"]], url=url)
    if not _PART_NUMBER.fullmatch(raw_number) and not image:
        return None
    # A number-image row intentionally has no guessed number, even when a
    # nearby price link exposes a seemingly article-like opaque identifier.
    raw_number = raw_number if _PART_NUMBER.fullmatch(raw_number) else ""
    position = _cell_text(cells, headers, "position") or title[0]
    name = _cell_text(cells, headers, "name") or title[1]
    raw_quantity = _cell_text(cells, headers, "quantity")
    if headers.get("quantity_columns"):
        raw_quantity = ""
    native_id = node.attrs.get("id") or f"{position}:{raw_number or image}"
    notes = _cell_text(cells, headers, "notes")
    raw_conditions = _space(
        " ".join(
            _cell_text(cells, headers, key) for key in ("conditions", "production", "transmission", "engine", "notes")
        )
    )
    row: dict[str, Any] = {
        "id": native_id,
        "row_id": native_id,
        "entity_kind": "part",
        "label": name,
        "name": name,
        "url": url,
        "ref_params": dict(parse_qsl(urlsplit(url).query)),
        "raw_number": raw_number or None,
        "normalized_number": re.sub(r"[^A-Za-z0-9]", "", raw_number).upper() if raw_number else None,
        "brand": entry.get("brand"),
        "position": position or None,
        "quantity": _quantity(raw_quantity),
        "raw_quantity": raw_quantity or None,
        "variant": _cell_text(cells, headers, "variant")
        or ("Economy" if re.search(r"\bEconomy\b", name, re.I) else None),
        "notes": notes,
        "confirmed": False,
    }
    _row_restrictions(row, _space(" ".join(filter(None, (inherited, raw_conditions)))))
    row.update(_placement(" ".join((inherited, name, notes))))
    inherited_conditions, inherited_unparsed = _conditions(inherited)
    row["inherited_conditions"] = (
        [{"conditions": inherited_conditions, "raw_conditions": inherited, "source_url": url, "binding": "diagram"}]
        if inherited_conditions
        else []
    )
    row["inherited_restrictions"] = inherited_unparsed
    if image and not raw_number:
        row.update(
            number_image_url=image,
            ocr_required=True,
            number_state="rendered_image",
            unparsed_conditions=list(dict.fromkeys([*row["unparsed_conditions"], "number_requires_rendered_evidence"])),
        )
    if quantities:
        start = headers.get("quantity", len(cells))
        row["quantity_by_modification"] = {
            label: {"raw": cells[start + index].text(), "quantity": _quantity(cells[start + index].text())}
            for index, label in enumerate(quantities)
            if start + index < len(cells)
        }
        row["unparsed_conditions"].append("quantity_depends_on_catalog_modification")
    return row


def _parts_rows(root: _Node, *, url: str, entry: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    out: list[dict[str, Any]] = []
    warnings: list[str] = []
    inherited = _scheme_restrictions(root)
    for table in _nodes(root, "table"):
        headers: dict[str, int] = {}
        title = ("", "")
        quantities: list[str] = []
        for node in _nodes(table, "tr"):
            if _closest(node, "table") is not table:
                continue
            detected = _header(node)
            if "number" in detected:
                headers = detected
                continue
            if not headers:
                continue
            cells = _cells(node)
            if headers.get("quantity_columns") and not quantities and len(cells) == headers["quantity_columns"]:
                quantities = [re.sub(r"\s+", "", c.text()) for c in cells]
                continue
            if len(cells) == 1 and cells[0].attrs.get("colspan"):
                caption = re.fullmatch(r"\(([^)]+)\)\s*(.+)", cells[0].text())
                if caption:
                    title = (caption.group(1), caption.group(2))
                continue
            row = _part_row(
                node, headers=headers, title=title, inherited=inherited, url=url, entry=entry, quantities=quantities
            )
            if row:
                out.append(row)
            elif any(_cell_text(cells, headers, key) for key in ("name", "number")):
                warnings.append("non_number_row_preserved_as_catalog_note")
    return out, list(dict.fromkeys(warnings))


def _access_flags(root: _Node, rows: list[dict[str, Any]]) -> list[str]:
    text = root.text()
    title = " ".join(n.text() for n in _nodes(root, "title"))
    if re.search(r"captcha|just a moment|проверка браузера|verify you are human|access denied", title, re.I):
        return ["challenge"]
    if not rows and re.search(
        r"(?:необходим|требуется|need|must).*?(?:авторизац|войти|log\s*in|sign\s*in)|доступ.*?только.*?зарегистрирован",
        text,
        re.I,
    ):
        return ["auth_required"]
    if not rows and any("__doPostBack" in n.attrs.get("onchange", "") for n in _nodes(root, "select")):
        return ["unsupported_postback_navigation"]
    return ["rendered_evidence_required"] if any(r.get("ocr_required") for r in rows) else []


def parse_catalog_html(html: str, *, url: str, operation: str, entry: dict[str, Any]) -> dict[str, Any]:
    """Parse public HTML into unconfirmed catalog rows without performing I/O."""
    tree = _Tree()
    tree.feed(html)
    tree.close()
    page_kind = _kind(url)
    if page_kind == "parts" or operation == "parts":
        rows, warnings = _parts_rows(tree.root, url=url, entry=entry)
        page_kind = "parts"
    else:
        rows = _subgroup_rows(tree.root, url=url)
        if rows:
            warnings = []
            page_kind = "diagrams"
        else:
            rows, warnings = _navigation_rows(tree.root, url=url, page_kind=page_kind)
    flags = _access_flags(tree.root, rows)
    if not rows and not flags:
        warnings.append("no_supported_public_catalog_rows")
    unparsed = list(dict.fromkeys(condition for row in rows for condition in row.get("unparsed_conditions", [])))
    return {
        "rows": rows,
        "page_kind": page_kind,
        "warnings": list(dict.fromkeys(warnings)),
        "access_flags": flags,
        "unparsed_conditions": unparsed,
    }
