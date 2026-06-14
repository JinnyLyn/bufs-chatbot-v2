import os
import re
import glob
import config
from pathlib import Path
from langchain_core.documents import Document
from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter

# A markdown header that introduces a cohort / admission-year section, e.g.
# "## 2017~2020학번", "## 2021학번", "### 2026학번". These mark HARD parent boundaries so a
# cohort's graduation requirements never get merged onto the previous cohort's tail.
_COHORT_HEADER_RE = re.compile(r"^#{1,6}\s*\d{4}\s*(?:~\s*\d{4})?\s*학번")

# Tag prefixed to each denormalized 학사일정 event line. __expand_calendar_rows emits it and
# __create_child_chunks detects it to split those lines into isolated child chunks — kept as a
# single constant so the two sites never drift.
_CAL_EVENT_TAG = "[일정]"


class DocumentChuncker:
    def __init__(self):
        self.__parent_splitter = MarkdownHeaderTextSplitter(
            headers_to_split_on=config.HEADERS_TO_SPLIT_ON, 
            strip_headers=False
        )
        self.__child_splitter = RecursiveCharacterTextSplitter(
            chunk_size=config.CHILD_CHUNK_SIZE, 
            chunk_overlap=config.CHILD_CHUNK_OVERLAP
        )
        self.__min_parent_size = config.MIN_PARENT_SIZE
        self.__max_parent_size = config.MAX_PARENT_SIZE

    def create_chunks(self, path_dir=config.MARKDOWN_DIR):
        all_parent_chunks, all_child_chunks = [], []

        for doc_path_str in sorted(glob.glob(os.path.join(path_dir, "*.md"))):
            doc_path = Path(doc_path_str)
            parent_chunks, child_chunks = self.create_chunks_single(doc_path)
            all_parent_chunks.extend(parent_chunks)
            all_child_chunks.extend(child_chunks)
        
        return all_parent_chunks, all_child_chunks

    def create_chunks_single(self, md_path):
        doc_path = Path(md_path)
        
        with open(doc_path, "r", encoding="utf-8") as f:
            raw = self.__forward_fill_month_tables(f.read())
        raw = self.__expand_calendar_rows(raw)
        parent_chunks = self.__parent_splitter.split_text(raw)
        
        merged_parents = self.__merge_small_parents(parent_chunks)
        split_parents = self.__split_large_parents(merged_parents)
        cleaned_parents = self.__clean_small_chunks(split_parents)
        
        all_parent_chunks, all_child_chunks = [], []
        self.__create_child_chunks(all_parent_chunks, all_child_chunks, cleaned_parents, doc_path)
        return all_parent_chunks, all_child_chunks

    @staticmethod
    def __starts_cohort_section(text: str) -> bool:
        """True when the chunk's first non-empty line is a cohort header (## 2017~2020학번)."""
        for line in text.splitlines():
            s = line.strip()
            if not s:
                continue
            return bool(_COHORT_HEADER_RE.match(s))
        return False

    @staticmethod
    def __expand_cohort_ranges(text: str) -> str:
        """Append the enumerated years after a *range* cohort header so a query for any single
        year in the range matches it. e.g. "## 2017~2020학번" → "## 2017~2020학번 (2017학번 2018학번
        2019학번 2020학번)". Without this, "2020학번" never lexically/semantically hits "2017~2020학번".
        """
        def repl(m):
            start, end = int(m.group("a")), int(m.group("b"))
            if 0 < end - start <= 12:
                years = " ".join(f"{y}학번" for y in range(start, end + 1))
                return f"{m.group(0)} ({years})"
            return m.group(0)
        return re.sub(r"(?m)^#{1,6}\s*(?P<a>\d{4})\s*~\s*(?P<b>\d{4})\s*학번.*$", repl, text)

    def __merge_small_parents(self, chunks):
        if not chunks:
            return []

        merged, current = [], None

        for chunk in chunks:
            # Cohort sections are HARD boundaries: flush the running parent before a new
            # cohort header so each 학번's requirements start clean and never inherit the
            # previous cohort's trailing "졸업학점 130학점" line (the bug that made the 2020
            # answer conflate cohorts).
            if current is not None and self.__starts_cohort_section(chunk.page_content):
                merged.append(current)
                current = None

            if current is None:
                current = chunk
            else:
                current.page_content += "\n\n" + chunk.page_content
                for k, v in chunk.metadata.items():
                    if k in current.metadata:
                        current.metadata[k] = f"{current.metadata[k]} -> {v}"
                    else:
                        current.metadata[k] = v

            if len(current.page_content) >= self.__min_parent_size:
                merged.append(current)
                current = None

        if current:
            # Keep a leftover cohort start standalone rather than folding it back into the
            # previous (different) cohort.
            if merged and not self.__starts_cohort_section(current.page_content):
                merged[-1].page_content += "\n\n" + current.page_content
                for k, v in current.metadata.items():
                    if k in merged[-1].metadata:
                        merged[-1].metadata[k] = f"{merged[-1].metadata[k]} -> {v}"
                    else:
                        merged[-1].metadata[k] = v
            else:
                merged.append(current)

        return merged

    def __split_large_parents(self, chunks):
        split_chunks = []

        for chunk in chunks:
            if len(chunk.page_content) <= self.__max_parent_size:
                split_chunks.append(chunk)
            else:
                # Table-aware: never split inside a markdown table, so cohort/requirement
                # tables stay whole in one parent.
                for piece in self.__table_aware_split(chunk.page_content):
                    split_chunks.append(Document(page_content=piece, metadata=dict(chunk.metadata)))

        return split_chunks

    @staticmethod
    def __forward_fill_month_tables(text: str) -> str:
        """Forward-fill the leading '월'(month) column of academic-calendar tables.

        The 학사일정 table flattens a rowspan: the 월 cell is filled only on the first row of
        each month and left empty ('||...') for the rest, so a split-off child like
        "||8(월)~12(금)|기말고사|" loses its month and the LLM guesses it wrong (e.g. 5월/8월
        instead of 6월). Fill each empty 월 cell with the last non-empty value so every row
        carries its month. Scoped to tables whose header is |월|일|...| so other tables are
        untouched.
        """
        out, in_cal, last_month = [], False, None
        for ln in text.split("\n"):
            s = ln.strip()
            if s.startswith("|"):
                parts = s.split("|")            # outer pipes -> empty strings at both ends
                if parts and parts[0] == "":
                    parts = parts[1:]
                if parts and parts[-1] == "":
                    parts = parts[:-1]
                cells = [c.strip() for c in parts]  # preserves internal empty (month) cells
                if len(cells) >= 2 and cells[0] == "월" and cells[1].startswith("일"):
                    in_cal, last_month = True, None
                    out.append(ln)
                    continue
                if in_cal:
                    if cells and set("".join(cells)) <= set("-: "):  # |---|---| separator row
                        out.append(ln)
                        continue
                    if cells[0] == "" and last_month is not None:
                        cells[0] = last_month
                    elif cells[0]:
                        last_month = cells[0]
                    out.append("| " + " | ".join(cells) + " |")
                    continue
            else:
                in_cal, last_month = False, None
            out.append(ln)
        return "\n".join(out)

    @staticmethod
    def __expand_calendar_rows(text: str) -> str:
        """Denormalize 학사일정 table rows into per-event sentences (tagged "[일정] ") appended
        after the table. A row like "| 6 | 8(월) ~ 12(월) | 기말고사기간 |" splits the date across
        the 월/일 columns, so neither dense retrieval ("기말고사 기간" vs bare numbers) nor the LLM
        (reconstructing "6월 8일" from cells "6"/"8") handles it. We emit
        "[일정] 2026학년도 1학기 기말고사기간: 6월 8일(월) ~ 12일(월)" — the academic year/semester
        (from the section header) + event + a contiguous "M월 D일" date. __create_child_chunks
        then splits each "[일정]" line into its OWN child chunk so the specific date ranks first
        instead of being diluted in a multi-event blob. Scoped to |월|일|…| tables.
        """
        out, in_cal, rows, sem = [], False, [], ""

        def flush():
            if rows:
                out.append("")
                out.extend(rows)
                rows.clear()

        for ln in text.split("\n"):
            s = ln.strip()
            if s.startswith("#"):
                m = re.search(r"\d{4}\s*학년도\s*\d\s*학기", s)
                if m:
                    sem = re.sub(r"\s+", " ", m.group()).strip()
            if s.startswith("|"):
                cells = [c.strip() for c in s.strip("|").split("|")]
                if len(cells) >= 2 and cells[0] == "월" and cells[1].startswith("일"):
                    in_cal = True
                    out.append(ln)
                    continue
                if in_cal:
                    if cells and set("".join(cells)) <= set("-: "):
                        out.append(ln)
                        continue
                    if len(cells) >= 3 and re.fullmatch(r"\d{1,2}", cells[0]) and cells[2]:
                        month, day, event = cells[0], cells[1], cells[2]
                        day_il = re.sub(r"(\d+)\(", r"\1일(", day)   # 8(월) -> 8일(월)
                        prefix = f"{sem} " if sem else ""
                        rows.append(f"{_CAL_EVENT_TAG} {prefix}{event}: {month}월 {day_il}")
                    out.append(ln)
                    continue
            if in_cal:
                flush()
                in_cal = False
            out.append(ln)
        if in_cal:
            flush()
        return "\n".join(out)

    @staticmethod
    def __is_table_line(line: str) -> bool:
        return line.lstrip().startswith("|")

    def __table_aware_split(self, text):
        """Split `text` into <= max_parent_size pieces without breaking markdown tables.

        A contiguous table (header + rows) is kept atomic; a table that alone exceeds the
        limit is split by rows with its header (first 2 lines) repeated on each piece.
        Non-table prose that is too large falls back to a recursive character split.
        """
        max_size = self.__max_parent_size
        overlap = config.CHILD_CHUNK_OVERLAP
        lines = text.split("\n")

        # Group consecutive lines into table / non-table segments.
        segments = []  # list[(is_table: bool, lines: list[str])]
        cur_is_table, cur = None, []
        for ln in lines:
            is_t = self.__is_table_line(ln)
            if cur and is_t != cur_is_table:
                segments.append((cur_is_table, cur))
                cur = []
            cur_is_table, cur = is_t, cur + [ln]
        if cur:
            segments.append((cur_is_table, cur))

        chunks, buf = [], []

        def buf_len():
            return sum(len(x) + 1 for x in buf)

        def flush():
            if buf:
                chunks.append("\n".join(buf))
                buf.clear()

        for is_table, seg_lines in segments:
            seg_text = "\n".join(seg_lines)
            if buf_len() + len(seg_text) <= max_size:
                buf.extend(seg_lines)
            elif is_table and len(seg_text) > max_size:
                flush()
                header = seg_lines[:2] if len(seg_lines) >= 2 else seg_lines
                piece = list(header)
                for row in seg_lines[len(header):]:
                    if sum(len(x) + 1 for x in piece) + len(row) > max_size and len(piece) > len(header):
                        chunks.append("\n".join(piece))
                        piece = list(header)
                    piece.append(row)
                if len(piece) > len(header):
                    chunks.append("\n".join(piece))
            elif not is_table and len(seg_text) > max_size:
                flush()
                chunks.extend(
                    RecursiveCharacterTextSplitter(chunk_size=max_size, chunk_overlap=overlap).split_text(seg_text)
                )
            else:
                flush()
                buf.extend(seg_lines)
        flush()
        return [c for c in chunks if c.strip()]

    def __clean_small_chunks(self, chunks):
        cleaned = []
        
        for i, chunk in enumerate(chunks):
            if len(chunk.page_content) < self.__min_parent_size:
                starts_cohort = self.__starts_cohort_section(chunk.page_content)
                if starts_cohort:
                    cleaned.append(chunk)  # cohort sections stay standalone — no cross-cohort glue
                elif cleaned:
                    cleaned[-1].page_content += "\n\n" + chunk.page_content
                    for k, v in chunk.metadata.items():
                        if k in cleaned[-1].metadata:
                            cleaned[-1].metadata[k] = f"{cleaned[-1].metadata[k]} -> {v}"
                        else:
                            cleaned[-1].metadata[k] = v
                elif i < len(chunks) - 1:
                    chunks[i + 1].page_content = chunk.page_content + "\n\n" + chunks[i + 1].page_content
                    for k, v in chunk.metadata.items():
                        if k in chunks[i + 1].metadata:
                            chunks[i + 1].metadata[k] = f"{v} -> {chunks[i + 1].metadata[k]}"
                        else:
                            chunks[i + 1].metadata[k] = v
                else:
                    cleaned.append(chunk)
            else:
                cleaned.append(chunk)
        
        return cleaned

    def __create_child_chunks(self, all_parent_pairs, all_child_chunks, parent_chunks, doc_path):
        for i, p_chunk in enumerate(parent_chunks):
            parent_id = f"{doc_path.stem}_parent_{i}"
            # Make single-year queries (e.g. "2020학번") match range headers ("2017~2020학번").
            p_chunk.page_content = self.__expand_cohort_ranges(p_chunk.page_content)
            p_chunk.metadata.update({"source": str(doc_path.stem)+".pdf", "parent_id": parent_id})

            all_parent_pairs.append((parent_id, p_chunk))

            # Calendar "[일정] …" lines become their OWN isolated child chunks so the specific
            # date ranks #1 instead of being diluted in a packed multi-event chunk. The lines
            # stay in the stored parent (above) so retrieve_parent still gives full context; we
            # remove them from the body that gets the normal recursive split to avoid crowding.
            body_lines, event_children = [], []
            for ln in p_chunk.page_content.split("\n"):
                if ln.strip().startswith(_CAL_EVENT_TAG):
                    sentence = ln.strip()[len(_CAL_EVENT_TAG):].strip()
                    if sentence:
                        event_children.append(Document(page_content=sentence, metadata=dict(p_chunk.metadata)))
                else:
                    body_lines.append(ln)
            body = Document(page_content="\n".join(body_lines), metadata=dict(p_chunk.metadata))
            all_child_chunks.extend(self.__child_splitter.split_documents([body]))
            all_child_chunks.extend(event_children)