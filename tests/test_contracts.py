"""Offline tests for the v1.5.0 contracts: QA parity, report schema, MCP surface."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
sys.path.insert(0, str(TOOLS))

import deliverable_qa as qa  # noqa: E402
import report_schema  # noqa: E402

BUILD_SPEC = importlib.util.spec_from_file_location("contract_build", TOOLS / "cad-fond-de-plan-build.py")
assert BUILD_SPEC is not None and BUILD_SPEC.loader is not None
build = importlib.util.module_from_spec(BUILD_SPEC)
BUILD_SPEC.loader.exec_module(build)

MCP_SPEC = importlib.util.spec_from_file_location("contract_mcp", ROOT / "server" / "mcp_server.py")
assert MCP_SPEC is not None and MCP_SPEC.loader is not None
mcp_server = importlib.util.module_from_spec(MCP_SPEC)
MCP_SPEC.loader.exec_module(mcp_server)

# --- QA contract -----------------------------------------------------------


def test_layout_resolver_maps_human_labels_to_layout_names():
    assert qa.ExpectedLayoutResolver.sheet_label_to_layout("A3 1:250") == "A3-1_250"
    assert qa.ExpectedLayoutResolver.sheet_label_to_layout("A0 1:1000") == "A0-1_1000"


def test_single_layout_report_is_judged_against_its_own_declaration():
    """The MCP path emits one layout; the QA contract must not demand all three."""
    report = {"paper_layouts": {"sheets": ["A3 1:250"]}}
    assert qa.ExpectedLayoutResolver.from_report(report) == {"A3-1_250"}


def test_report_without_layout_declaration_keeps_conservative_expectation():
    assert qa.ExpectedLayoutResolver.from_report({}) == qa.ALL_LAYOUTS
    assert qa.ExpectedLayoutResolver.from_report(None) == qa.ALL_LAYOUTS


def test_png_pixel_expectation_derives_from_report_sheet_size():
    """Legacy square reports still resolve to a square, but never to a scalar."""
    assert qa.ExpectedLayoutResolver.expected_png_px({"outputs": {"sheet_mm": 420}}) == (4961, 4961)
    assert qa.ExpectedLayoutResolver.expected_png_px(None) == (qa.DEFAULT_PNG_PX, qa.DEFAULT_PNG_PX)
    assert qa.ExpectedLayoutResolver.expected_png_px({"outputs": {"sheet_mm": "bad"}}) == (
        qa.DEFAULT_PNG_PX,
        qa.DEFAULT_PNG_PX,
    )


def test_png_pixel_expectation_is_per_axis_for_a_declared_iso_page():
    """v1.9.0: A3 is 420x297, so the raster is 4961x3508, not a 4961 square."""
    report = {"outputs": {"sheet": {"page_mm": {"width": 420.0, "height": 297.0}}}}
    assert qa.ExpectedLayoutResolver.expected_png_px(report) == (4961, 3508)
    assert qa.ExpectedLayoutResolver.declared_page_mm(report) == (420.0, 297.0)


def test_missing_directory_reports_every_absent_deliverable(tmp_path):
    result = qa.qa_output_directory(tmp_path / "absent")
    assert result["ok"] is False
    assert "not found" in result["findings"][0]


def test_partial_directory_lists_expected_deliverables(tmp_path):
    (tmp_path / qa.HOLLOW_SVG).write_text("<svg/>", encoding="utf-8")
    result = qa.qa_output_directory(tmp_path)
    assert result["ok"] is False
    missing = [f for f in result["findings"] if f.startswith("missing deliverable:")]
    assert len(missing) == len(qa.ALL_DELIVERABLES) - 1


def test_small_file_is_rejected_but_sizeable_file_passes(tmp_path):
    tiny = tmp_path / "tiny.svg"
    tiny.write_text("x", encoding="utf-8")
    assert qa.qa_output_directory(tiny)["ok"] is False
    ok_file = tmp_path / "ok.svg"
    ok_file.write_text("<svg>" + " " * 500 + "</svg>", encoding="utf-8")
    assert qa.qa_output_directory(ok_file)["ok"] is True


def test_non_monochrome_svg_is_a_finding(tmp_path):
    svg = tmp_path / qa.HOLLOW_SVG
    svg.write_text('<svg width="420mm" height="420mm"><path fill="#FF0000"/></svg>', encoding="utf-8")
    findings = qa._svg_findings(svg, 420)
    assert any("non-monochrome fill" in finding for finding in findings)


def test_svg_prohibits_text_elements(tmp_path):
    svg = tmp_path / qa.HOLLOW_SVG
    svg.write_text('<svg width="420mm" height="420mm"><text x="1">label</text></svg>', encoding="utf-8")
    findings = qa._svg_findings(svg, 420)
    assert any("prohibited text element" in finding for finding in findings)


def test_svg_accepts_alternate_sheet_size_when_declared(tmp_path):
    svg = tmp_path / qa.HOLLOW_SVG
    svg.write_text('<svg width="841mm" height="841mm"><path fill="#000000"/></svg>', encoding="utf-8")
    assert qa._svg_findings(svg, 841) == []
    assert any("841" in finding for finding in qa._svg_findings(svg, 420))


def test_pdf_signature_is_checked(tmp_path):
    good = tmp_path / qa.PDF
    good.write_bytes(b"%PDF-1.7\n")
    assert qa._pdf_findings(good) == []
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"not a pdf")
    assert qa._pdf_findings(bad)


def test_report_findings_flag_epsg_mismatch_and_missing_qa_verdict():
    report = {"center": {"epsg": 32630}, "qa": {"ok": False}, "building_regularization": {}, "attribution": ""}
    findings = qa._report_findings(report, 32631)
    joined = " | ".join(findings)
    assert "does not match report" in joined
    assert "passing QA verdict" in joined
    assert "no regularized buildings" in joined
    assert "missing source attribution" in joined


def test_qa_contract_version_is_present_in_results(tmp_path):
    assert qa.qa_output_directory(tmp_path / "absent")["qa_contract_version"] == qa.QA_CONTRACT_VERSION


# --- report schema ---------------------------------------------------------


def test_provenance_block_declares_schema_and_engine_version():
    block = report_schema.provenance_block(engine_version="9.9.9", generated_at="2026-11-05T00:00:00+00:00")
    assert block["report_schema_version"] == report_schema.REPORT_SCHEMA_VERSION
    assert block["engine_version"] == "9.9.9"
    assert block["report_detail"] == "full"


def test_file_digest_is_stable_and_reports_size(tmp_path):
    target = tmp_path / "tile.csv"
    target.write_text("a,b\n1,2\n", encoding="utf-8")
    first = report_schema.file_digest(target)
    second = report_schema.file_digest(target)
    assert first == second
    assert first["bytes"] == target.stat().st_size
    assert len(first["sha256"]) == 64


def test_file_digest_returns_none_for_missing_input(tmp_path):
    assert report_schema.file_digest(tmp_path / "nope.csv") is None
    assert report_schema.file_digest(None) is None


def test_input_provenance_skips_unreadable_files(tmp_path):
    present = tmp_path / "present.csv"
    present.write_text("x", encoding="utf-8")
    entries = report_schema.input_provenance([present, tmp_path / "absent.csv"], role="score_thresholds_csv")
    assert len(entries) == 1
    assert entries[0]["role"] == "score_thresholds_csv"
    assert entries[0]["name"] == "present.csv"


def _sample_report() -> dict:
    return {
        "ok": True,
        "footprints": {
            "provider": "google-open-buildings",
            "output_provenance": [{"source_id": f"row-{i}"} for i in range(5)],
            "source_feature_thresholds": [{"source_id": f"row-{i}"} for i in range(5)],
        },
        "provenance": {"engine_version": "1.5.0", "generated_at_utc": "2026-11-05T00:00:00+00:00"},
    }


def test_summary_detail_omits_per_feature_arrays_and_records_counts():
    trimmed = report_schema.apply_detail(_sample_report(), "summary")
    footprints = trimmed["footprints"]
    assert "output_provenance" not in footprints
    assert footprints["output_provenance_omitted"]["count"] == 5
    assert "source_feature_thresholds" not in footprints
    assert footprints["source_feature_thresholds_omitted"]["count"] == 5
    assert trimmed["provenance"]["report_detail"] == "summary"


def test_full_detail_preserves_per_feature_arrays():
    report = _sample_report()
    kept = report_schema.apply_detail(report, "full")
    assert len(kept["footprints"]["output_provenance"]) == 5
    assert kept["provenance"]["report_detail"] == "full"


def test_unknown_detail_mode_is_rejected():
    with pytest.raises(ValueError, match="detail must be one of"):
        report_schema.apply_detail(_sample_report(), "everything")


def test_reports_are_deterministic_except_declared_volatile_fields():
    first = _sample_report()
    second = json.loads(json.dumps(_sample_report()))
    second["provenance"]["generated_at_utc"] = "2027-01-01T00:00:00+00:00"
    assert report_schema.is_deterministic(first, second) is True
    second["footprints"]["provider"] = "microsoft"
    assert report_schema.is_deterministic(first, second) is False


# --- builder wiring --------------------------------------------------------


def test_builder_parser_exposes_report_detail_modes():
    parser = build.build_parser()
    assert parser.parse_args([]).report_detail == "full"
    assert parser.parse_args(["--report-detail", "summary"]).report_detail == "summary"
    with pytest.raises(SystemExit):
        parser.parse_args(["--report-detail", "verbose"])


def test_builder_engine_version_matches_package_version():
    assert build.ENGINE_VERSION == report_schema.package_version(default=build.ENGINE_VERSION)


def test_builder_qa_wrapper_uses_shared_contract(tmp_path):
    """A directory without deliverables must fail through the shared contract."""
    result = build.qa_outputs(tmp_path, 3, 7, 32630, 4961, expected_layouts=["A3-1_250"])
    assert result["ok"] is False
    assert result["qa_contract_version"] == qa.QA_CONTRACT_VERSION
    assert result["orthogonalized_buildings"] == 3
    assert result["clipped_hatch_segments"] == 7


# --- MCP surface parity ----------------------------------------------------


def test_mcp_exposes_provider_and_precision_parameters():
    fields = mcp_server.generate_cad_plan.__annotations__
    for name in (
        "footprint_provider",
        "footprint_file",
        "osm_file",
        "study_radius_m",
        "google_min_confidence",
        "google_target_precision",
        "google_thresholds_file",
    ):
        assert name in fields


def test_mcp_command_forwards_provider_flags(tmp_path):
    source = tmp_path / "google.csv"
    source.write_text("latitude,longitude\n", encoding="utf-8")
    thresholds = tmp_path / "thresholds.csv"
    thresholds.write_text("s2_token\n", encoding="utf-8")
    command = mcp_server._build_command(
        lat=35.7,
        lon=-0.641,
        radius=120.0,
        output_dir=tmp_path / "out",
        provider=mcp_server.FootprintProvider.GOOGLE_OPEN_BUILDINGS,
        sheet_size="A3",
        denominator=250,
        orientation=mcp_server.SheetOrientation.LANDSCAPE,
        google_min_confidence=0.65,
        target_precision=mcp_server.TargetPrecision.P90,
        footprint_file=source,
        osm_file=None,
        thresholds_file=thresholds,
    )
    joined = " ".join(command)
    assert "--footprint-provider google-open-buildings" in joined
    assert "--google-target-precision 90" in joined
    assert "--google-thresholds-path" in joined
    assert "--google-open-buildings-file" in joined
    assert "--report-detail summary" in joined


def test_mcp_command_uses_microsoft_flag_for_default_provider(tmp_path):
    source = tmp_path / "ms.csv.gz"
    source.write_text("x", encoding="utf-8")
    command = mcp_server._build_command(
        lat=35.7,
        lon=-0.641,
        radius=120.0,
        output_dir=tmp_path / "out",
        provider=mcp_server.FootprintProvider.MICROSOFT,
        sheet_size="A3",
        denominator=250,
        orientation=mcp_server.SheetOrientation.LANDSCAPE,
        google_min_confidence=0.65,
        target_precision=None,
        footprint_file=source,
        osm_file=None,
        thresholds_file=None,
    )
    joined = " ".join(command)
    assert "--microsoft-file" in joined
    assert "--google-target-precision" not in joined


def test_mcp_rejects_precision_without_google_provider():
    with pytest.raises(ValueError, match="requires footprint_provider"):
        mcp_server.generate_cad_plan(
            lat=35.7,
            lon=-0.641,
            scale="1:250",
            sheet_size="A3",
            google_target_precision=mcp_server.TargetPrecision.P90,
        )


def test_mcp_rejects_precision_without_cached_thresholds():
    with pytest.raises(ValueError, match="requires google_thresholds_file"):
        mcp_server.generate_cad_plan(
            lat=35.7,
            lon=-0.641,
            scale="1:250",
            sheet_size="A3",
            footprint_provider=mcp_server.FootprintProvider.GOOGLE_OPEN_BUILDINGS,
            google_target_precision=mcp_server.TargetPrecision.P85,
        )


def test_mcp_rejects_missing_cached_input(tmp_path):
    with pytest.raises(FileNotFoundError, match="footprint_file does not exist"):
        mcp_server.generate_cad_plan(
            lat=35.7, lon=-0.641, scale="1:250", sheet_size="A3", footprint_file=str(tmp_path / "absent.csv")
        )


def test_mcp_supported_formats_lists_providers_and_tiers():
    supported = mcp_server.list_supported_formats()
    assert supported.footprint_providers == ["microsoft", "google-open-buildings"]
    assert supported.target_precisions == [80, 85, 90]


def test_mcp_topology_report_uses_shared_contract(tmp_path, monkeypatch):
    monkeypatch.setenv("CAD_FOND_DE_PLAN_ALLOWED_ROOT", str(tmp_path))
    dxf = tmp_path / "fond-de-plan-cad.dxf"
    dxf.write_text("not a real dxf", encoding="utf-8")
    report = mcp_server.verify_topology(str(dxf))
    assert report.valid is False
    assert report.qa_contract_version == qa.QA_CONTRACT_VERSION
    assert any("DXF parse failed" in finding for finding in report.diagnostics)


def test_mcp_topology_rejects_paths_outside_allowed_root(tmp_path, monkeypatch):
    monkeypatch.setenv("CAD_FOND_DE_PLAN_ALLOWED_ROOT", str(tmp_path / "allowed"))
    outside = ROOT / "pyproject.toml"
    with pytest.raises(ValueError, match="allowed root"):
        mcp_server.verify_topology(str(outside))


# --- v1.8.1: D2 metadata tolerates an absent site area ----------------------


def test_metadata_from_utm_accepts_a_missing_ground_area():
    """D2: a run without --image has no site polygon and used to crash with TypeError."""
    import layout_engine

    metadata = layout_engine.metadata_from_utm(655239.131, 3861554.193, 32630, None)
    assert metadata["ground_area_m2"] is None
    assert metadata["utm"]["epsg"] == "EPSG:32630"
    assert 34.8 < metadata["wgs84"]["latitude"] < 34.9


def test_metadata_from_utm_still_reports_a_real_area():
    import layout_engine

    metadata = layout_engine.metadata_from_utm(655239.131, 3861554.193, 32630, 1234.5)
    assert metadata["ground_area_m2"] == pytest.approx(1234.5)


def test_cartouche_renders_not_specified_without_an_area():
    import layout_engine

    text = layout_engine.cartouche_text(layout_engine.metadata_from_utm(655239.131, 3861554.193, 32630, None), 500)
    assert any("NOT SPECIFIED" in line for line in text)


# --- v1.8.1: D3 builder and checker agree on layouts ------------------------


def test_layout_less_report_declares_no_layouts_and_is_judged_on_that():
    """D3: the builder passed an empty set (vacuous pass) while check.py demanded A3/A1/A0."""
    report = {"paper_layouts": {"sheets": []}, "outputs": {"sheet_mm": 420.0}}
    assert qa.ExpectedLayoutResolver.from_report(report) == frozenset()


def test_report_declaring_a_single_sheet_is_judged_against_it_only():
    report = {"paper_layouts": {"sheets": ["A3 1:250"]}, "outputs": {"sheet_mm": 420.0}}
    assert qa.ExpectedLayoutResolver.from_report(report) == frozenset({"A3-1_250"})


def test_absent_report_keeps_the_conservative_legacy_expectation():
    assert qa.ExpectedLayoutResolver.from_report(None) == qa.ALL_LAYOUTS


def test_builder_forwards_an_empty_declaration_instead_of_disabling_the_check(tmp_path):
    """An empty expectation must reach the DXF check, not be replaced by `or ()`."""
    captured = {}

    def fake_qa_directory(path, *, include_report, expected_layouts, expected_png_px, declared_page_mm=None):
        captured["expected_layouts"] = expected_layouts
        return {"ok": True, "findings": [], "dxf": {}}

    original = build.qa_output_directory
    build.qa_output_directory = fake_qa_directory
    try:
        build.qa_outputs(tmp_path, 0, 0, 32630, 4961, expected_layouts=[])
    finally:
        build.qa_output_directory = original
    assert captured["expected_layouts"] == frozenset()


def test_dxf_without_layouts_passes_when_none_are_expected(tmp_path):
    import ezdxf

    doc = ezdxf.new("R2018", setup=True)
    doc.header["$INSUNITS"] = 6
    doc.header["$USERI1"] = 32630
    path = tmp_path / "no-layout.dxf"
    doc.saveas(path)
    findings = qa.qa_dxf_file(path, frozenset())["findings"]
    assert not any("paper layouts" in finding for finding in findings)


# --- v1.8.1: D4 no provenance claim without a supplied aerial ---------------


def test_warnings_do_not_claim_a_reference_aerial_that_was_not_supplied():
    warnings = build.build_warnings(None, 0.12, _Provider(), "osm-api-map-bbox", [])
    joined = " ".join(warnings)
    assert "No reference aerial supplied" in joined
    assert "Reference JPEG has no EXIF" not in joined


def test_warnings_do_mention_the_aerial_when_one_was_supplied(tmp_path):
    warnings = build.build_warnings(tmp_path / "aerial.jpg", 0.12, _Provider(), "overpass", [])
    joined = " ".join(warnings)
    assert "Reference JPEG has no EXIF" in joined
    assert "No reference aerial supplied" not in joined


def test_warnings_only_claim_the_overpass_fallback_when_it_happened():
    fallback = " ".join(build.build_warnings(None, 0.12, _Provider(), "osm-api-map-bbox", []))
    direct = " ".join(build.build_warnings(None, 0.12, _Provider(), "overpass", []))
    assert "Overpass endpoints were unavailable" in fallback
    assert "Overpass endpoints were unavailable" not in direct


def test_attribution_omits_a_user_aerial_without_one():
    """D4: a print deliverable must not credit an aerial that was never supplied.

    Builds the attribution string the same way ``build()`` does, with and without
    an image, and asserts the suffix tracks the input rather than being constant.
    """
    import inspect

    source = inspect.getsource(build.build)
    assert 'attribution = f"{OSM_ATTRIBUTION}; {provider.attribution}"' in source
    assert 'attribution += "; reference aerial supplied by user."' in source
    assert "if image_path:\n        attribution +=" in source


class _Provider:
    name = "microsoft"
    attribution = "Microsoft Global ML Building Footprints (CDLA-Permissive-2.0)"


# --- v1.9.0: orientation parity across the MCP surface ----------------------


def test_mcp_exposes_a_landscape_default_orientation():
    default = mcp_server.SheetOrientation.LANDSCAPE
    assert default.value == "landscape"
    assert mcp_server.SheetOrientation.PORTRAIT.value == "portrait"


def test_mcp_command_forwards_portrait_orientation(tmp_path):
    source = tmp_path / "tile.csv.gz"
    source.write_bytes(b"{}")
    command = mcp_server._build_command(
        lat=34.884528,
        lon=-1.30125,
        radius=300.0,
        output_dir=tmp_path / "out",
        provider=mcp_server.FootprintProvider.MICROSOFT,
        sheet_size="A0",
        denominator=1000,
        orientation=mcp_server.SheetOrientation.PORTRAIT,
        google_min_confidence=0.65,
        target_precision=None,
        footprint_file=source,
        osm_file=None,
        thresholds_file=None,
    )
    joined = " ".join(command)
    assert "--orientation portrait" in joined
