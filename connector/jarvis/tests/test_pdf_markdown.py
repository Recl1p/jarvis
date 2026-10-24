"""Tests for tools/pdf_markdown.py: every attached PDF is converted to a Markdown file before the
agent analyzes it (dashboard composer attachments and messaging-platform documents alike)."""

import os
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from jarvis import pdf_markdown
from tools import read_extract
from tools.read_extract import ExtractionError


class TestIsPdf(unittest.TestCase):
    def test_extension_case_insensitive(self):
        self.assertTrue(pdf_markdown.is_pdf("Book.PDF"))
        self.assertTrue(pdf_markdown.is_pdf("/cache/doc_abc_notes.pdf"))
        self.assertFalse(pdf_markdown.is_pdf("notes.docx"))
        self.assertFalse(pdf_markdown.is_pdf("pdf"))


class TestConversion(unittest.TestCase):
    def test_bytes_wraps_extractor_markdown_in_titled_document(self):
        with mock.patch.object(read_extract, "extract_document_bytes",
                               return_value="## Chapter 1\n\nReal text.\n") as extract:
            md = pdf_markdown.pdf_bytes_to_markdown(b"%PDF-1.4", "My Book.pdf")
        extract.assert_called_once_with(b"%PDF-1.4", "My Book.pdf")
        self.assertTrue(md.startswith("# My Book.pdf\n\n"))
        self.assertIn("Converted from PDF to Markdown", md)
        self.assertIn("## Chapter 1\n\nReal text.", md)
        self.assertTrue(md.endswith("\n"))

    def test_bytes_falls_back_to_pdftotext_pages_when_extractor_fails(self):
        """anydoc missing (or rejecting a PDF poppler can read) must still yield Markdown."""
        with mock.patch.object(read_extract, "extract_document_bytes",
                               side_effect=ExtractionError("Unsupported document type")), \
             mock.patch.object(pdf_markdown, "_bytes_page_texts",
                               return_value=["First page text\n", "   \n", "Third page\n"]):
            md = pdf_markdown.pdf_bytes_to_markdown(b"%PDF-1.4", "scan.pdf")
        self.assertIn("## Page 1\n\nFirst page text", md)
        self.assertIn("## Page 2\n\n_(no text layer on this page)_", md)
        self.assertIn("## Page 3\n\nThird page", md)

    def test_partly_scanned_pdf_keeps_the_readable_pages(self):
        """Stock Hermes answers a PDF with some scanned pages with only its NEEDS OCR note; the readable pages'
        text must still reach the agent, under that note."""
        note = ("[NEEDS OCR: pages 2 of this PDF are scanned images with no text layer — their content is "
                "MISSING below. If the missing pages matter: render just those pages.]\n")
        with mock.patch.object(read_extract, "extract_document_bytes", return_value=note), \
             mock.patch.object(pdf_markdown, "_bytes_page_texts", return_value=["Page one text\n", "", "Page three\n"]):
            md = pdf_markdown.pdf_bytes_to_markdown(b"%PDF-1.4", "mixed.pdf")
        self.assertIn("[NEEDS OCR: pages 2", md)
        self.assertIn("## Page 1\n\nPage one text", md)
        self.assertIn("## Page 2\n\n_(no text layer on this page)_", md)
        self.assertIn("## Page 3\n\nPage three", md)

    def test_bytes_reraises_when_nothing_is_readable(self):
        with mock.patch.object(read_extract, "extract_document_bytes",
                               side_effect=ExtractionError("MalformedError: bad xref")), \
             mock.patch.object(pdf_markdown, "_bytes_page_texts", return_value=None):
            with self.assertRaises(ExtractionError):
                pdf_markdown.pdf_bytes_to_markdown(b"junk", "broken.pdf")
        with mock.patch.object(read_extract, "extract_document_bytes",
                               side_effect=ExtractionError("x")), \
             mock.patch.object(pdf_markdown, "_bytes_page_texts", return_value=[" ", "\n"]):
            with self.assertRaises(ExtractionError):
                pdf_markdown.pdf_bytes_to_markdown(b"junk", "blank.pdf")

    def test_file_conversion_uses_path_extractors(self):
        with mock.patch.object(read_extract, "extract_document_text",
                               side_effect=ExtractionError("x")), \
             mock.patch.object(read_extract, "_pdf_page_texts", return_value=["Only page"]) as pages:
            md = pdf_markdown.pdf_file_to_markdown("/cache/doc_1_2_report.pdf", "report.pdf")
        pages.assert_called_once_with("/cache/doc_1_2_report.pdf")
        self.assertTrue(md.startswith("# report.pdf\n\n"))
        self.assertIn("## Page 1\n\nOnly page", md)


class TestSaveMarkdown(unittest.TestCase):
    def test_writes_sanitized_md_under_unique_dir(self):
        with TemporaryDirectory() as tmp:
            path = pdf_markdown.save_markdown("# x\n", "../../etc/pass wd?.pdf", Path(tmp))
            self.assertEqual(path.suffix, ".md")
            self.assertEqual(path.name, "pass wd_.md")
            self.assertEqual(path.parent.parent, Path(tmp))
            self.assertRegex(path.parent.name, r"^[0-9a-f]{32}$")
            self.assertEqual(path.read_text(encoding="utf-8"), "# x\n")

    def test_default_directory_is_under_hermes_home(self):
        with TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"HERMES_HOME": tmp}):
            path = pdf_markdown.save_markdown("# x\n", "a.pdf")
            self.assertTrue(str(path).startswith(str(Path(tmp) / "attachments" / "pdf-markdown")))

    def test_prunes_only_expired_entry_dirs(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            old = base / ("a" * 32)
            fresh = base / ("b" * 32)
            unrelated = base / "keep-me"
            for d in (old, fresh, unrelated):
                d.mkdir()
            stale = time.time() - pdf_markdown.PDF_MARKDOWN_RETENTION_SECONDS - 60
            os.utime(old, (stale, stale))
            os.utime(unrelated, (stale, stale))
            pdf_markdown.save_markdown("# x\n", "new.pdf", base)
            self.assertFalse(old.exists())
            self.assertTrue(fresh.exists())
            self.assertTrue(unrelated.exists())


class TestSiblingMarkdownFile(unittest.TestCase):
    def test_writes_sibling_and_reuses_it(self):
        with TemporaryDirectory() as tmp:
            pdf = os.path.join(tmp, "doc_1_2_book.pdf")
            Path(pdf).write_bytes(b"%PDF-1.4")
            with mock.patch.object(pdf_markdown, "pdf_file_to_markdown",
                                   return_value="# book.pdf\n\nbody\n") as convert:
                md, md_path = pdf_markdown.convert_pdf_file_to_markdown_file(pdf, "book.pdf")
                self.assertEqual(md_path, os.path.join(tmp, "doc_1_2_book.md"))
                self.assertEqual(Path(md_path).read_text(encoding="utf-8"), md)
                again, _ = pdf_markdown.convert_pdf_file_to_markdown_file(pdf, "book.pdf")
            convert.assert_called_once()
            self.assertEqual(again, md)

    def test_stale_sibling_is_regenerated(self):
        with TemporaryDirectory() as tmp:
            pdf = os.path.join(tmp, "book.pdf")
            md_file = os.path.join(tmp, "book.md")
            Path(md_file).write_text("old", encoding="utf-8")
            Path(pdf).write_bytes(b"%PDF-1.4")
            os.utime(md_file, (time.time() - 3600, time.time() - 3600))
            with mock.patch.object(pdf_markdown, "pdf_file_to_markdown", return_value="new\n"):
                md, _ = pdf_markdown.convert_pdf_file_to_markdown_file(pdf)
            self.assertEqual(md, "new\n")
            self.assertEqual(Path(md_file).read_text(encoding="utf-8"), "new\n")


if __name__ == "__main__":
    unittest.main()
