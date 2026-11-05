# Notes for agents

`README.md` belongs to the human. Changes to behaviour that it doesn't cover yet are recorded here.

## Automatic OCR

`archiver run` now reads PDFs with no text layer with tesseract, on its own, so `archiver ocr` is no longer a required step.

- It covers web PDFs (lane `generic`, downloaded again to a temporary file) and local files (lane `localfile`, read in place and never changed), limited to the lanes the run includes. `--only youtube` does no OCR, and `--only localfile` leaves web PDFs alone.
- It starts once those lanes finish, so a long scan holds up nothing. It doesn't wait for the YouTube lane.
- The first run also works through PDFs left over from earlier runs. Each PDF is tried once. Ctrl-C stops it at once, and the PDF in progress is tried again next run.
- `--no-ocr` turns it off, `--ocr-lang eng+tur` sets the languages, and `--workers` sets how many PDFs run at a time.
- If tesseract or a language pack is missing, the run logs a warning and carries on, and those PDFs wait for a later run without using up their try.

`archiver ocr` remains for `--retry` and `--limit`, and for OCR on its own. It now also exits with a message when a language pack is missing.

`README.md` still describes OCR as a separate step, in the quick start, the local-files section and "OCR (`archiver ocr`)".
