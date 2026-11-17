"""View models of the other built-in element types (no GTK, stdlib only, no
chemotion_api): research plan, wellplate, screen, cell line, device
description and sequence based macromolecule sample (SBMM).

Same contract as element_views.sample_view (see its module docstring and the
M7 design): an adapter is `(data, images, url) -> view model` with the raw
server JSON, the images the helper loaded (`{key: {"mime", "data"[, "encoding"]}}`)
and the element's web link; `IMAGE_REFS[type](data)` says which server paths
the helper should GET (`{key: "/images/..."}`). Every value is a string,
empty values and sections are left out. Table sections may carry `targets`
(one `{"type", "id"}` or None per row), text sections `markup`, image sections
`{"kind": "image", "image"}`.

Each adapter follows the main tabs of the web client (ELN 4.0.1,
app/javascript/src/apps/mydb/elements/details/<type>/): labels, grouping and
order of its properties tab. The JSON of the ELN 3.1.x line (chemotion_api
v312) is handled by the presence of keys (it has no cell line, device
description or SBMM).
"""
from __future__ import annotations

from typing import Any, Callable

import element_views as ev

MAX_TABLE_ROWS = 200  # a wellplate may have 10 000 wells
ATTACHMENT_IMAGE = "/api/v1/attachments/image/-1?identifier={}"  # ResearchPlanDetailsFieldImage.js
LEGACY_IMAGE = "/images/research_plans/{}"


def _s(value: Any, digits: int = 6) -> str:
    """A value as a display string: numbers without trailing zeros, booleans
    as yes/no, lists joined, None as ""."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float)):
        return ev.fmt_number(value, digits)
    if isinstance(value, (list, tuple)):
        return ", ".join(part for part in (_s(item, digits) for item in value) if part)
    if isinstance(value, dict):
        return ""
    return str(value).strip()


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _labels(data: dict) -> str:
    return ev._user_labels(data) if hasattr(ev, "_user_labels") else ""


def _wrap(type_name: str, data: dict, url: str, title: str, subtitle: str, sections: list,
          notes: list[str] | None = None) -> dict:
    return {"type": type_name, "id": data.get("id"), "updated_at": str(data.get("updated_at") or ""),
            "title": title, "subtitle": subtitle, "url": url or "", "image": None,
            "sections": [section for section in sections if section], "notes": notes or []}


def _join(*parts: str) -> str:
    return " · ".join(part for part in parts if part)


def _target(type_name: str, element_id: Any) -> dict | None:
    return {"type": type_name, "id": element_id} if element_id is not None else None


def _cap(rows: list, targets: list, notes: list[str], what: str) -> tuple[list, list]:
    if len(rows) > MAX_TABLE_ROWS:
        notes.append(f"Only the first {MAX_TABLE_ROWS} of {len(rows)} {what} are shown.")
        return rows[:MAX_TABLE_ROWS], targets[:MAX_TABLE_ROWS]
    return rows, targets


# -- research plan --------------------------------------------------------------
# details/researchPlans/ResearchPlanDetails.js (tab "Research plan": name, then
# researchPlanTab/ResearchPlanDetailsBody.js with its fields in order) and
# wellplatesTab/.

def _rp_image_path(value: dict) -> str:
    """ResearchPlanDetailsFieldImage.generateSrcOfImage: a public name with a
    dot is a legacy file below /images/research_plans, else an attachment."""
    name = str(value.get("public_name") or "")
    if not name or name.startswith("blob"):
        return ""
    return LEGACY_IMAGE.format(name) if "." in name else ATTACHMENT_IMAGE.format(name)


def research_plan_image_refs(data: dict) -> dict[str, str]:
    refs: dict[str, str] = {}
    for field in _list(data.get("body")):
        if not isinstance(field, dict) or field.get("id") is None:
            continue
        value = _dict(field.get("value"))
        if field.get("type") == "image":
            path = _rp_image_path(value)
        elif field.get("type") == "ketcher" and value.get("svg_file"):
            path = LEGACY_IMAGE.format(value["svg_file"])  # ResearchPlanDetailsFieldKetcher.js
        else:
            continue
        if path:
            refs[f"body:{field['id']}"] = path
    return refs


def _cell(value: Any) -> str:
    return "" if value is False else _s(value)  # an unchecked box is an empty cell


def _rp_table(value: dict, title: str) -> dict | None:
    """A body table: columns (headerName, field/colId) and rows by field."""
    columns = [c for c in _list(value.get("columns")) if isinstance(c, dict)]
    keys = [str(c.get("field") or c.get("colId") or "") for c in columns]
    names = [str(c.get("headerName") or key) for c, key in zip(columns, keys)]
    rows = [[_cell(_dict(row).get(key)) for key in keys] for row in _list(value.get("rows"))]
    return ev.table(title, names, rows) if columns else None


def research_plan_view(data: dict, images: dict | None = None, url: str = "") -> dict:
    images = images or {}
    notes: list[str] = []
    sections: list = []
    counts: dict[str, int] = {}

    def title_of(kind: str, field: dict, default: str) -> str:
        counts[kind] = counts.get(kind, 0) + 1
        title = str(field.get("title") or default) if kind == "richtext" else default
        return title if counts[kind] == 1 or kind == "richtext" else f"{title} {counts[kind]}"

    for field in _list(data.get("body")):
        if not isinstance(field, dict):
            continue
        kind = field.get("type")
        value = field.get("value")
        if kind == "richtext":
            sections.append(ev.text(title_of(kind, field, "Text"), value))
        elif kind == "table":
            sections.append(_rp_table(_dict(value), title_of(kind, field, "Table")))
        elif kind in ("image", "ketcher"):
            key = f"body:{field.get('id')}"
            default = "Image" if kind == "image" else "Structure"
            title = title_of(kind, field, default)
            if kind == "image" and _dict(value).get("file_name"):
                title = f"{title}: {_dict(value)['file_name']}"
            image = images.get(key)
            if image:
                sections.append(ev.image_section(title, image))
            elif key in research_plan_image_refs(data):
                notes.append(f"{title} could not be loaded and is not shown.")
        elif kind in ("sample", "reaction"):
            element_id = _dict(value).get(f"{kind}_id")
            if element_id is not None:
                label = kind.capitalize()
                sections.append(ev.table(title_of(kind, field, label), [label],
                                         [[f"{kind} id {_s(element_id)}"]], [_target(kind, element_id)]))
        elif kind:
            notes.append(f"A field of type \"{kind}\" is not shown yet.")
    wellplates = [w for w in _list(data.get("wellplates")) if isinstance(w, dict)]
    sections.append(ev.table("Wellplates", ["Name", "Short label"],
                             [[_s(w.get("name")), _s(w.get("short_label"))] for w in wellplates],
                             [_target("wellplate", w.get("id")) for w in wellplates]))
    short_label = _s(data.get("short_label"))
    head = ev.fields("Research plan", [("Labels", _labels(data), "")])
    return _wrap("research_plan", data, url, _s(data.get("name")) or short_label, _join(short_label),
                 [head] + sections, notes)


# -- wellplate ------------------------------------------------------------------
# details/wellplates/WellplateDetails.js: tabs Designer, List, Properties; the
# list tab (listTab/WellplateList.js) has the columns used here, the properties
# tab (propertiesTab/WellplateProperties.js) name, size, readouts, description.

ROW_LABELS = [chr(ord("A") + i) for i in range(26)]


def row_label(index: int) -> str:
    """Wellplate.rowLabel: 1 = A … 26 = Z, 27 = AA … (0 or less: "")."""
    if index <= 0:
        return ""
    label = ""
    while index > 0:  # bijective base 26, as AA follows Z
        index, rest = divmod(index - 1, 26)
        label = ROW_LABELS[rest] + label
    return label


def well_position(well: dict) -> str:
    """Well.alphanumericPosition: row letter(s) of y, then the column x."""
    position = _dict(well.get("position"))
    x, y = position.get("x"), position.get("y")
    if not isinstance(x, int) or not isinstance(y, int) or x <= 0 or y <= 0:
        return "n/a"
    return f"{row_label(y)}{x}"


def _sample_formula(sample: dict) -> str:
    """Sample.molecule_formula."""
    if sample.get("decoupled"):
        return _s(sample.get("sum_formula"))
    return _s(_dict(sample.get("molecule")).get("sum_formular"))


def _well_filled(well: dict) -> bool:
    if well.get("sample"):
        return True
    return any(_s(_dict(r).get("value")) for r in _list(well.get("readouts")))


def wellplate_view(data: dict, images: dict | None = None, url: str = "") -> dict:
    notes: list[str] = []
    width, height = data.get("width"), data.get("height")
    size = ""
    if isinstance(width, int) and isinstance(height, int) and width * height > 0:
        size = f"{width * height} ({width}x{height})"  # WellplateSizeDropdown
    elif isinstance(data.get("size"), int) and data["size"] > 0:
        size = str(data["size"])  # ELN 3.1.x names only the well count
    titles = [_s(t) for t in _list(data.get("readout_titles"))]
    wells = [w for w in _list(data.get("wells")) if isinstance(w, dict)]
    filled = [w for w in wells if _well_filled(w)]
    filled.sort(key=lambda w: (_dict(w.get("position")).get("y") or 0, _dict(w.get("position")).get("x") or 0))

    columns = ["Position", "Name", "External label", "Sum formula", "Molarity (M)"]
    with_additive = any(_s(w.get("additive")) for w in filled)
    if with_additive:
        columns.append("Additive")
    for index, title in enumerate(titles):
        columns.append(title or f"Readout {index + 1}")
    rows, targets = [], []
    for well in filled:
        sample = _dict(well.get("sample"))
        molarity = sample.get("molarity_value")
        row = [well_position(well), _s(sample.get("short_label")), _s(sample.get("external_label")),
               _sample_formula(sample), _s(molarity, 4) if molarity else ""]
        if with_additive:
            row.append(_s(well.get("additive")))
        readouts = _list(well.get("readouts"))
        for index in range(len(titles)):
            readout = _dict(readouts[index]) if index < len(readouts) else {}
            value = _s(readout.get("value"))
            row.append(" ".join(part for part in (value, _s(readout.get("unit"))) if part) if value else "")
        rows.append(row)
        targets.append(_target("sample", sample.get("id")) if sample else None)
    rows, targets = _cap(rows, targets, notes, "filled wells")
    sections = [
        ev.fields("Wellplate", [("Size", size, ""), ("Readouts", ", ".join(t for t in titles if t), ""),
                                ("Labels", _labels(data), "")]),
        ev.text("Description", data.get("description")),
    ]
    if wells:
        sections.append(ev.table(f"Wells ({len(filled)} of {len(wells)} filled)", columns, rows, targets))
    short_label = _s(data.get("short_label"))
    return _wrap("wellplate", data, url, _s(data.get("name")) or short_label, _join(short_label, size),
                 sections, notes)


# -- screen ---------------------------------------------------------------------
# details/screens/ScreenDetails.js: name, collaborator, requirements, conditions,
# result, description, then the wellplates (ScreenWellplates.js: name and
# description); tab "Research Plans".

def screen_view(data: dict, images: dict | None = None, url: str = "") -> dict:
    wellplates = [w for w in _list(data.get("wellplates")) if isinstance(w, dict)]
    plans = [p for p in _list(data.get("research_plans")) if isinstance(p, dict)]
    sections = [
        ev.fields("Screen", [("Collaborator", _s(data.get("collaborator")), ""),
                             ("Requirements", _s(data.get("requirements")), ""),
                             ("Conditions", _s(data.get("conditions")), ""),
                             ("Result", _s(data.get("result")), ""),
                             ("Labels", _labels(data), "")]),
        ev.text("Description", data.get("description")),
        ev.table("Wellplates", ["Name", "Description"],
                 [[_s(w.get("name")), ev.delta_text(w.get("description"))] for w in wellplates],
                 [_target("wellplate", w.get("id")) for w in wellplates]),
        ev.table("Research plans", ["Name", "Short label"],
                 [[_s(p.get("name")), _s(p.get("short_label"))] for p in plans],
                 [_target("research_plan", p.get("id")) for p in plans]),
    ]
    short_label = _s(data.get("short_label"))
    return _wrap("screen", data, url, _s(data.get("name")) or short_label, short_label, sections)


# -- cell line ------------------------------------------------------------------
# details/cellLines/propertiesTab/GeneralProperties.js: the accordions "Common
# Properties" (the material) and "Sample specific properties".

BIOSAFETY = {"S0": "Biosafety level 0", "S1": "Biosafety level 1", "S2": "Biosafety level 2",
             "S3": "Biosafety level 3"}


def cell_line_view(data: dict, images: dict | None = None, url: str = "") -> dict:
    material = _dict(data.get("cellline_material"))
    level = _s(material.get("biosafety_level"))
    sections = [
        ev.fields("Common properties", [
            ("Cell line name", _s(material.get("name")), ""), ("Source", _s(material.get("source")), ""),
            ("Disease", _s(material.get("disease")), ""), ("Organism", _s(material.get("organism")), ""),
            ("Mutation", _s(material.get("mutation")), ""), ("Variant", _s(material.get("variant")), ""),
            ("Tissue", _s(material.get("tissue")), ""), ("Growth medium", _s(material.get("growth_medium")), ""),
            ("Biosafety level", BIOSAFETY.get(level, level), ""),
            ("Cryopreservation medium", _s(material.get("cryo_pres_medium")), ""),
            ("Opt. growth temperature", _s(material.get("optimal_growth_temp")), ""),
            ("Gender", _s(material.get("gender")), ""), ("Cell type", _s(material.get("cell_type")), ""),
            ("Material description", _s(material.get("description")), ""),
        ]),
        ev.fields("Sample specific properties", [
            ("Amount", _s(data.get("amount")), _s(data.get("unit"))),
            ("Passage", _s(data.get("passage")), ""),
            ("Name of specific sample", _s(data.get("name")), ""),
            ("Contamination", _s(data.get("contamination")), ""),
            ("Sample description", _s(data.get("description")), ""),
        ]),
    ]
    short_label = _s(data.get("short_label"))
    name = _s(data.get("name")) or _s(material.get("name"))
    return _wrap("cell_line", data, url, name, _join(short_label, _s(material.get("name")),
                                                      _s(material.get("source"))), sections)


# -- device description ---------------------------------------------------------
# details/deviceDescriptions/propertiesTab/PropertiesForm.js (tab "Properties":
# name, device class, device type, then the accordions in order) and
# maintenanceTab/MaintenanceForm.js (tab "Maintenance").

def _rows_table(title: str, items: Any, columns: list[tuple[str, str]]) -> dict | None:
    items = [item for item in _list(items) if isinstance(item, dict)]
    return ev.table(title, [label for _, label in columns],
                    [[_s(item.get(key)) for key, _ in columns] for item in items])


def device_description_view(data: dict, images: dict | None = None, url: str = "") -> dict:
    def f(label: str, key: str, unit: str = "") -> tuple:
        return (label, _s(data.get(key)), unit)

    weight = _s(data.get("weight"))
    sections = [
        ev.fields("Device", [f("Name", "name"), f("Device class", "device_class"),
                             f("Device class detail", "device_class_detail"), f("Operation mode", "operation_mode"),
                             f("Device type name", "device_type_name"), f("Device type ID", "device_type_id"),
                             f("Device type ID type", "device_type_id_type"), ("Labels", _labels(data), "")]),
        ev.fields("General description", [
            f("Model name", "vendor_device_name"), f("Model ID", "vendor_device_id"), f("Serial no", "serial_number"),
            f("Manufacturer name", "vendor_company_name"), f("Manufacturer ID", "vendor_id"),
            f("Manufacturer ID type", "vendor_id_type"), f("Description", "description"),
            f("Tags", "general_tags")]),
        ev.fields("Version specific information", [
            f("Version", "version_number"), f("Started: Installation date", "version_installation_start_date"),
            f("End date", "version_installation_end_date"), f("Identifier type", "version_identifier_type"),
            f("Identifier (DOI)", "version_doi"), f("Identifier (DOI) link", "version_doi_url"),
            f("Characterization of this version", "version_characterization")]),
        _rows_table("Operators", data.get("operators"), [
            ("name", "Name"), ("phone", "Phone"), ("email", "eMail"), ("type", "Type"), ("comment", "Comment")]),
        ev.fields("Owner", [f("Institution", "owner_institution"), f("eMail", "owner_email"), f("ID", "owner_id"),
                            f("ID type", "owner_id_type")]),
        ev.fields("Location", [f("Institution", "university_campus"), f("Institute", "institute"),
                               f("Building", "building"), f("Room", "room")]),
        ev.fields("Inventory", [f("Inventory ID", "inventory_id"),
                                f("Alternative identifier / label", "alternative_identifier")]),
        ev.fields("Access options", [f("Infrastructure assignment", "infrastructure_assignment"),
                                     f("Access options", "access_options"), f("Comments", "access_comments")]),
        ev.fields("Software and interfaces", [f("Application name", "application_name"),
                                              f("Version", "application_version"), f("Vendor URL", "vendor_url")]),
        ev.fields("Manuals, documentation and helpers", [
            ("Additional helpers uploaded to attachments", "yes" if data.get("helpers_uploaded") else "", ""),
            f("Policies and user information", "policies_and_user_information")]),
        ev.fields("Information for publications", [f("Description for methods part", "description_for_methods_part")]),
        ev.fields("Physical descriptions", [f("Size", "size"), ("Weight", weight, _s(data.get("weight_unit")) if weight else "")]),
        # Maintenance tab
        ev.fields("Maintenance", [f("Maintenance contract available", "maintenance_contract_available"),
                                  f("Scheduling", "maintenance_scheduling")]),
        _rows_table("Contact for maintenance and repair", data.get("contact_for_maintenance"), [
            ("company", "Company"), ("contact", "Contact"), ("email", "eMail"), ("phone", "Phone"),
            ("comment", "Comment")]),
        _rows_table("Planned maintenance", data.get("planned_maintenance"), [
            ("date", "Date"), ("type", "Type"), ("details", "Details"), ("status", "Status"), ("costs", "Costs"),
            ("time", "Time"), ("changes", "Changes")]),
        _rows_table("Consumables needed for maintenance", data.get("consumables_needed_for_maintenance"), [
            ("name", "Name"), ("type", "Type"), ("number", "Number"), ("status", "Status"), ("costs", "Costs"),
            ("details", "Details")]),
        _rows_table("Unexpected maintenance and repair", data.get("unexpected_maintenance"), [
            ("date", "Date"), ("type", "Type"), ("details", "Details"), ("status", "Status"), ("costs", "Costs"),
            ("time", "Time"), ("changes", "Changes")]),
        ev.fields("Unexpected power shutdown", [
            f("Measures after complete power shutdown", "measures_after_full_shut_down"),
            f("Measures for a running device with short shutdown period", "measures_after_short_shut_down")]),
        ev.fields("Planned offline period", [
            f("Measures to plan an offline period", "measures_to_plan_offline_period"),
            f("Re-Start after planned offline time", "restart_after_planned_offline_period")]),
    ]
    notes = []
    if _list(data.get("ontologies")):
        notes.append("The ontologies of the Details tab are not shown yet.")
    short_label = _s(data.get("short_label"))
    return _wrap("device_description", data, url, _s(data.get("name")) or short_label,
                 _join(short_label, _s(data.get("device_class"))), sections, notes)


# -- sequence based macromolecule sample ----------------------------------------
# details/sequenceBasedMacromoleculeSamples/propertiesTab/ (PropertiesForm.js:
# "General description" and "Sample Characteristics"; ReferenceAndModificationForm.js:
# the protein identifiers; SequenceAndPostTranslationalModificationForm.js).

SBMM_LABELS = {
    "sbmm_type": {"protein": "Protein"},
    "sbmm_subtype": {"unmodified": "Unmodified", "glycoprotein": "Glycoprotein"},
    "uniprot_derivation": {"uniprot_unknown": "Does not exist",
                           "uniprot": "Protein used as described in Uniprot / reference",
                           "uniprot_modified": "Used modified protein"},
    "function_or_application": {"enzyme": "Enzyme", "hormone": "Hormone", "structural": "Structural",
                                "component": "Component", "energy_source": "Energy source"},
    "obtained_by": {"purchased": "Purchased", "self_produced": "Self Produced"},
    "formulation": {"dissolved": "Dissolved", "solid": "Solid"},
    "heterologous_expression": {"yes": "Yes", "no": "No", "unknown": "Unknown"},
}
PROTEIN_MODIFICATIONS = [("modification_n_terminal", "N-terminal"), ("modification_c_terminal", "C-terminal"),
                         ("modification_insertion", "Insertion"), ("modification_deletion", "Deletion"),
                         ("modification_mutation", "Mutation"), ("modification_other", "Others")]
POST_TRANSLATIONAL = [
    ("phosphorylation", "Phosphorylation", [("ser", "Ser"), ("thr", "Thr"), ("tyr", "Tyr")]),
    ("glycosylation", "Glycosylation", [("n_linked_asn", "N-linked Asn"), ("o_linked_lys", "O-linked Lys"),
                                        ("o_linked_ser", "O-linked Ser"), ("o_linked_thr", "O-linked Thr")]),
    ("acetylation", "Acetylation", []),
    ("hydroxylation", "Hydroxylation", [("lys", "Lys"), ("pro", "Pro")]),
    ("methylation", "Methylation", [("arg", "Arg"), ("glu", "Glu"), ("lys", "Lys")]),
]
# (label, value key, unit key), PropertiesForm.js "Sample Characteristics"
SBMM_AMOUNTS = [("Concentration", "concentration"), ("Molarity", "molarity"),
                ("Concentration (room temp.)", "concentration_rt"),
                ("Activity in U/L", "activity_per_volume"), ("Activity in U/g", "activity_per_mass"),
                ("Volume as used", "volume_as_used"), ("Amount as used (mol)", "amount_as_used_mol"),
                ("Amount as used (mass)", "amount_as_used_mass"), ("Activity", "activity")]


def _choice(key: str, value: Any) -> str:
    text = _s(value)
    return SBMM_LABELS.get(key, {}).get(text, text)


def _modifications(sbmm: dict) -> list:
    rows = []
    sequence = _dict(sbmm.get("protein_sequence_modifications"))
    for key, label in PROTEIN_MODIFICATIONS:
        if sequence.get(key):
            rows.append((label, _s(sequence.get(f"{key}_details")) or "yes", ""))
    post = _dict(sbmm.get("post_translational_modifications"))
    for key, label, parts in POST_TRANSLATIONAL:
        if not post.get(f"{key}_enabled"):
            continue
        details = [f"{name}: {_s(post.get(f'{key}_{part}_details')) or 'yes'}" for part, name in parts
                   if post.get(f"{key}_{part}_enabled")]
        if key == "acetylation" and post.get("acetylation_lysin_number") is not None:
            details.append(f"Lysin No: {_s(post['acetylation_lysin_number'])}")
        rows.append((label, "; ".join(details) or "yes", ""))
    if post.get("other_modifications_enabled"):
        rows.append(("Other modifications", _s(post.get("other_modifications_details")) or "yes", ""))
    return rows


def sbmm_sample_view(data: dict, images: dict | None = None, url: str = "") -> dict:
    sbmm = _dict(data.get("sequence_based_macromolecule"))

    def both(key: str) -> Any:  # the sample's own value first (the ELN keeps it in either place)
        value = data.get(key)
        return value if value not in (None, "") else sbmm.get(key)

    sequence = _s(sbmm.get("sequence"))
    sections = [
        ev.fields("Protein", [
            ("Type", _choice("sbmm_type", sbmm.get("sbmm_type")), ""),
            ("Subtype of protein", _choice("sbmm_subtype", sbmm.get("sbmm_subtype")), ""),
            ("Derivation", _choice("uniprot_derivation", sbmm.get("uniprot_derivation")), ""),
            ("UniProt number", _s(sbmm.get("primary_accession")), ""),
            ("Own ID", _s(sbmm.get("own_identifier")), ""), ("Other reference ID", _s(sbmm.get("other_identifier")), ""),
            ("Short name", _s(sbmm.get("short_name")), ""), ("Full name", _s(sbmm.get("full_name")), ""),
            ("EC number", _s(sbmm.get("ec_numbers")), ""),
            ("Sequence length", _s(sbmm.get("sequence_length")), ""),
            ("Sequence mass", _s(sbmm.get("molecular_weight")), "kD"),
            ("Pdb DOI", _s(sbmm.get("pdb_doi")), ""), ("Link pdb", _s(sbmm.get("link_pdb")), ""),
            ("Link UniProt", _s(sbmm.get("link_uniprot")), ""),
            ("Heterologous expression", _choice("heterologous_expression", both("heterologous_expression")), ""),
            ("Organism", _s(both("organism")), ""), ("Taxon ID", _s(both("taxon_id")), ""),
            ("Strain", _s(both("strain")), ""), ("Tissue", _s(both("tissue")), ""),
            ("Localisation", _s(both("localisation")), "")]),
        ev.fields("Modifications", _modifications(sbmm)),
        {"title": "Sequence", "kind": "text", "text": sequence} if sequence else None,
        ev.fields("Sample characteristics", [
            ("Name", _s(data.get("name")), ""), ("Short label", _s(data.get("short_label")), ""),
            ("External label", _s(data.get("external_label")), ""),
            ("Function or application", _choice("function_or_application", data.get("function_or_application")), ""),
            ("Obtained by", _choice("obtained_by", data.get("obtained_by")), ""),
            ("Supplier", _s(data.get("supplier")), ""),
            *[(label, _s(data.get(f"{key}_value")), _s(data.get(f"{key}_unit")) if _s(data.get(f"{key}_value")) else "")
              for label, key in SBMM_AMOUNTS],
            ("Formulation", _choice("formulation", data.get("formulation")), ""),
            ("Purity", _s(data.get("purity")), ""), ("Purity detection", _s(data.get("purity_detection")), ""),
            ("Purification method", _s(data.get("purification_method")), ""),
            ("Labels", _labels(data), "")]),
    ]
    short_label = _s(data.get("short_label"))
    name = _s(data.get("name")) or _s(sbmm.get("short_name")) or short_label
    return _wrap("sequence_based_macromolecule_sample", data, url, name,
                 _join(short_label, _s(sbmm.get("short_name")) if _s(sbmm.get("short_name")) != name else ""), sections)


# -- registry -------------------------------------------------------------------

Adapter = Callable[[dict, dict, str], dict]
Refs = Callable[[dict], dict]

ADAPTERS: dict[str, Adapter] = {
    "research_plan": research_plan_view,
    "wellplate": wellplate_view,
    "screen": screen_view,
    "cell_line": cell_line_view,
    "device_description": device_description_view,
    "sequence_based_macromolecule_sample": sbmm_sample_view,
}
IMAGE_REFS: dict[str, Refs] = {"research_plan": research_plan_image_refs}
