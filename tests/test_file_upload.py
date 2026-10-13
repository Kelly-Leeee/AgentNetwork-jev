from __future__ import annotations

import io
import unittest
import zipfile

from agentrouter.server import _extract_uploaded_text


class FileUploadExtractionTests(unittest.TestCase):
    def test_extracts_utf8_txt(self) -> None:
        self.assertEqual(_extract_uploaded_text("notes.txt", "hello 世界".encode()), "hello 世界")

    def test_extracts_docx_paragraphs(self) -> None:
        document = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            '<w:body><w:p><w:r><w:t>First</w:t></w:r></w:p>'
            '<w:p><w:r><w:t>Second</w:t></w:r></w:p></w:body></w:document>'
        )
        content = io.BytesIO()
        with zipfile.ZipFile(content, "w") as archive:
            archive.writestr("word/document.xml", document)
        self.assertEqual(_extract_uploaded_text("brief.docx", content.getvalue()), "First\nSecond")

    def test_rejects_other_extensions(self) -> None:
        with self.assertRaisesRegex(ValueError, "Only"):
            _extract_uploaded_text("brief.pdf", b"content")


if __name__ == "__main__":
    unittest.main()
