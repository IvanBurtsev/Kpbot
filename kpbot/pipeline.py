"""Пайплайн: письмо → позиции → матчинг → расчёт → КП → черновик ответа."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from .config import Settings, settings as default_settings
from .domain import (
    LineStatus,
    Quote,
    RfqLine,
    RfqStatus,
    build_reply_subject,
    price_quote,
    status_for,
)
from .extract import RuleExtractor
from .llm import build_extractor
from .mailparse import ParsedEmail, parse_eml
from .matching import PriceIndex, match_lines
from .notify import Notifier
from .pricelist import default_price_items, load_price_items
from .render import html_to_pdf, render_quote_docx, render_template, write_html
from .store import Store, sha256_bytes

log = logging.getLogger("kpbot.pipeline")

# Предохранитель: если браузер в окружении не печатает, не долбим его на каждом письме.
_pdf_works: bool | None = None


class NotAnRfq(ValueError):
    """В письме не нашлось ни одной позиции с количеством — это не заявка.

    Важно не создавать заявку из рассылки: иначе реальный ящик с 1 000 писем
    превращается в 1 000 пустых карточек, и сервис становится бесполезным.
    """


@dataclass(slots=True)
class IngestResult:
    rfq_id: str
    status: str
    lines: int
    matched: int
    excluded: list[str] = field(default_factory=list)
    total: str = "0.00"
    html_path: str = ""
    docx_path: str = ""
    pdf_path: str = ""
    pdf_error: str = ""
    duplicate: bool = False
    reply_text: str = ""


class Pipeline:
    def __init__(self, store: Store, cfg: Settings | None = None) -> None:
        self.store = store
        self.cfg = cfg or default_settings
        self.extractor = build_extractor(self.cfg)
        self.notifier = Notifier.from_settings(self.cfg)
        self._index: PriceIndex | None = None
        self._index_key: tuple[int, str] | None = None

    def price_index(self) -> PriceIndex:
        """Индекс прайса кэшируется и пересобирается только при изменении прайса."""
        key = (self.store.price_count(), self.store.price_updated_at())
        if self._index is None or self._index_key != key:
            self._index = PriceIndex(self.store.price_items())
            self._index_key = key
            log.info("индекс прайса собран: %s позиций", len(self._index))
        return self._index

    # ---------- прайс ----------

    def ensure_price_list(self) -> int:
        """Подхватывает data/price_list.xlsx|csv, а если их нет — пишет демо-прайс."""
        if self.store.price_count() > 0:
            return self.store.price_count()

        for candidate in (self.cfg.data_dir / "price_list.xlsx", self.cfg.data_dir / "price_list.csv"):
            if candidate.exists():
                try:
                    items = load_price_items(candidate)
                    count = self.store.replace_price_items(items)
                    log.info("прайс загружен из %s: %s позиций", candidate.name, count)
                    return count
                except Exception as exc:  # битый прайс не должен ронять сервис
                    log.warning("не удалось прочитать %s: %s", candidate.name, exc)

        items = default_price_items()
        count = self.store.replace_price_items(items)
        log.info("загружен встроенный демо-прайс: %s позиций", count)
        return count

    def reload_price_list(self, path: Path) -> int:
        items = load_price_items(path)
        return self.store.replace_price_items(items)

    # ---------- приём письма ----------

    def process_eml_bytes(self, raw: bytes, *, actor: str = "system") -> IngestResult:
        parsed = parse_eml(raw)
        return self._process(parsed, actor=actor)

    def process_text(
        self,
        text: str,
        *,
        sender: str,
        subject: str,
        message_id: str | None = None,
        actor: str = "api",
    ) -> IngestResult:
        parsed = ParsedEmail(
            sender=(sender or "unknown@example.com")[:320],
            subject=(subject or "Заявка")[:500],
            message_id=(message_id or f"api-{uuid.uuid4().hex}")[:500],
            received_at="",
            text=text[: self.cfg.max_email_text_chars],
        )
        return self._process(parsed, actor=actor)

    def analyze(self, text: str) -> tuple[list[RfqLine], Quote, bool]:
        """Разбирает текст письма без записи в БД. Общая логика для poll и preview.

        Возвращает (строки, расчёт, похоже_ли_на_заявку).
        """
        index = self.price_index()
        lines = self.extractor.extract(text, max_lines=self.cfg.max_rfq_lines)
        skipped = list(getattr(self.extractor, "skipped", []))

        # Строки без количества, но с узнаваемой позицией прайса — человеку, не в мусор.
        lines.extend(self._promote_known_items(skipped, index, start=len(lines) + 1))
        is_request = bool(lines) or self._looks_like_request(text, index)

        lines = match_lines(
            lines,
            index,
            auto_threshold=self.cfg.match_auto_threshold,
            ambiguous_threshold=self.cfg.match_ambiguous_threshold,
        )
        lines = self._dedupe_by_sku(lines)
        quote = price_quote(
            lines,
            {item.sku: item for item in index.items},
            vat_rate=self.cfg.vat_rate,
            delivery=self.cfg.delivery_price,
            max_qty=self.cfg.max_qty,
        )
        return lines, quote, is_request

    def _process(self, parsed: ParsedEmail, *, actor: str) -> IngestResult:
        self.ensure_price_list()

        existing = self.store.find_rfq_by_message(parsed.message_id)
        if existing:
            rfq = self.store.get_rfq(existing) or {}
            quote = self.store.get_quote(existing) or {}
            return IngestResult(
                rfq_id=existing,
                status=str(rfq.get("status", "unknown")),
                lines=len(rfq.get("lines", [])),
                matched=sum(1 for ln in rfq.get("lines", []) if ln["status"] == "ok"),
                total=str(quote.get("total", "0.00")),
                duplicate=True,
            )

        rfq_id = uuid.uuid4().hex
        lines, quote, is_request = self.analyze(parsed.text)
        if not is_request:
            raise NotAnRfq("в письме не найдено ни одной позиции с количеством")

        status = status_for(quote)

        self.store.save_rfq(
            rfq_id=rfq_id,
            message_id=parsed.message_id,
            sender=parsed.sender,
            subject=parsed.subject,
            received_at=parsed.received_at,
            status=status.value,
            raw_text=parsed.text[: self.cfg.max_email_text_chars],
            lines=lines,
        )
        quote_id = uuid.uuid4().hex
        self.store.save_quote(quote_id, rfq_id, quote)

        html_path = self._render_documents(rfq_id, parsed, lines, quote)
        reply_text = build_reply_text(parsed, quote, rfq_id)

        self.store.log(
            rfq_id,
            "ingest",
            actor,
            f"строк={len(lines)}, в расчёте={len(quote.lines)}, исключено={quote.unmatched}, "
            f"экстрактор={getattr(self.extractor, 'last_source', 'rules')}",
        )
        if not quote.lines:
            self.store.log(
                rfq_id, "unparsed_request", "system",
                "позиции угадываются, но количество не распознано — отдано человеку",
            )

        self._notify_new(rfq_id, parsed, lines, quote)

        return IngestResult(
            rfq_id=rfq_id,
            status=status.value,
            lines=len(lines),
            matched=len(quote.lines),
            excluded=quote.excluded,
            total=str(quote.total),
            html_path=str(html_path.get("html") or ""),
            docx_path=str(html_path.get("docx") or ""),
            pdf_path=str(html_path.get("pdf") or ""),
            pdf_error=str(html_path.get("pdf_error") or ""),
            reply_text=reply_text,
        )

    def _promote_known_items(
        self, skipped: list[str], index, *, start: int
    ) -> list[RfqLine]:
        """Строки без количества, но с узнаваемой позицией прайса — человеку, не в мусор."""
        promoted: list[RfqLine] = []
        for segment in skipped[:50]:
            item, score = index.best(segment)
            if item is None or score < 0.55:
                continue
            promoted.append(
                RfqLine(
                    idx=start + len(promoted),
                    raw_text=segment[:300],
                    qty=None,
                    unit=None,
                    matched_sku=item.sku,
                    matched_name=item.name,
                    match_score=score,
                    status=LineStatus.INVALID,
                    note="не указано количество или единица измерения",
                )
            )
        return promoted

    def _notify_new(self, rfq_id: str, parsed: ParsedEmail, lines: list, quote: Quote) -> None:
        """Одно уведомление на заявку. Сбой не влияет на приём письма."""
        if not self.notifier.enabled:
            return
        if any(item["action"] == "notified" for item in self.store.decisions(rfq_id)):
            return
        sent = self.notifier.notify_new_rfq(
            rfq_id=rfq_id,
            subject=parsed.subject,
            sender=parsed.sender,
            total=str(quote.total),
            lines=len(lines),
            matched=len(quote.lines),
        )
        self.store.log(rfq_id, "notified" if sent else "notify_failed", "system", "")

    @staticmethod
    def _dedupe_by_sku(lines: list[RfqLine]) -> list[RfqLine]:
        """Одна позиция прайса не может попасть в КП дважды.

        Дубли появляются, когда правила и модель по-разному называют одну строку
        («гипсокартона 12.5» и «гипсокартон 12.5 мм»). Вторая строка не удаляется,
        а помечается человеку — иначе сумма тихо удвоится.
        """
        seen: dict[str, RfqLine] = {}
        result: list[RfqLine] = []
        for line in lines:
            if not line.matched_sku:
                result.append(line)
                continue
            first = seen.get(line.matched_sku)
            if first is None:
                seen[line.matched_sku] = line
                result.append(line)
                continue
            if first.qty is not None and line.qty is None:
                # обрывок той же позиции без количества: информации не несёт
                continue
            if line.status is not LineStatus.OK:
                result.append(line)
                continue
            if first.qty is not None and line.qty is not None and first.qty == line.qty:
                # тот же артикул в том же количестве — след второго экстрактора, удаляем
                continue
            line.status = LineStatus.INVALID
            line.note = f"дубль позиции {line.matched_sku} (см. строку {first.idx}), количества разные"
            result.append(line)

        for position, line in enumerate(result, start=1):
            line.idx = position
        return result

    def _looks_like_request(self, text: str, index) -> bool:
        """Заявка ли это вообще: ищем минимум две позиции прайса в тексте.

        Нужно, чтобы письмо «полторы упаковки саморезов 4.2х75 и десяток уголков»
        не считалось рассылкой и не терялось молча.
        """
        matches = 0
        for segment in RuleExtractor._segments(text)[:60]:
            _item, score = index.best(segment)
            if score >= 0.6:
                matches += 1
                if matches >= 2:
                    return True
        return False

    def apply_edits(self, rfq_id: str, lines: list[RfqLine], *, actor: str, note: str = "") -> None:
        """Сохраняет правки человека, пересчитывает КП и перерисовывает документы."""
        rfq = self.store.get_rfq(rfq_id)
        if rfq is None:
            raise KeyError(rfq_id)
        if rfq["status"] == RfqStatus.SENT.value:
            raise PermissionError("отправленную заявку менять нельзя")

        for position, line in enumerate(lines, start=1):
            line.idx = position
        self.store.replace_lines(rfq_id, lines)

        index = self.price_index()
        quote = price_quote(
            lines,
            {item.sku: item for item in index.items},
            vat_rate=self.cfg.vat_rate,
            delivery=self.cfg.delivery_price,
            max_qty=self.cfg.max_qty,
        )
        self.store.save_quote(uuid.uuid4().hex, rfq_id, quote)
        self.store.set_status(rfq_id, status_for(quote).value)

        parsed = ParsedEmail(
            sender=rfq["sender"],
            subject=rfq["subject"],
            message_id=rfq["message_id"],
            received_at=rfq["received_at"],
            text=rfq["raw_text"],
        )
        self._render_documents(rfq_id, parsed, lines, quote)
        self.store.log(
            rfq_id,
            "lines_edited",
            actor,
            f"{note}; строк={len(lines)}, в расчёте={len(quote.lines)}, итого={quote.total}",
        )

    # ---------- документы ----------

    def _render_documents(
        self, rfq_id: str, parsed: ParsedEmail, lines: list, quote: Quote
    ) -> dict[str, object]:
        global _pdf_works
        company = {
            "name": self.cfg.company_name,
            "inn": self.cfg.company_inn,
            "kpp": self.cfg.company_kpp,
            "address": self.cfg.company_address,
            "phone": self.cfg.company_phone,
            "email": self.cfg.company_email,
            "signer": self.cfg.company_signer,
        }
        client = {"sender": parsed.sender, "subject": parsed.subject}
        rfq_meta = {"id": rfq_id, "received_at": parsed.received_at}
        context = {
            "company": company,
            "client": client,
            "rfq": rfq_meta,
            "quote": quote,
            "lines": lines,
            "valid_days": self.cfg.quote_valid_days,
        }
        result: dict[str, object] = {"html": None, "docx": None, "pdf": None, "pdf_error": ""}

        try:
            html = render_template("kp.html", **context)
        except Exception as exc:  # шаблон не должен ронять приём письма
            log.exception("не удалось отрендерить КП")
            self.store.log(rfq_id, "render_error", "system", str(exc)[:500])
            return result

        html_path = write_html(self.cfg.out_dir / f"{rfq_id}.html", html)
        self.store.add_artifact(rfq_id, "kp_html", html_path, sha256_bytes(html_path.read_bytes()))
        result["html"] = html_path

        try:
            docx_path = render_quote_docx(
                self.cfg.out_dir / f"{rfq_id}.docx",
                company=company,
                client=client,
                rfq=rfq_meta,
                quote=quote,
                valid_days=self.cfg.quote_valid_days,
            )
            self.store.add_artifact(rfq_id, "kp_docx", docx_path, sha256_bytes(docx_path.read_bytes()))
            result["docx"] = docx_path
        except Exception as exc:
            log.warning("DOCX не создан: %s", exc)
            self.store.log(rfq_id, "docx_skipped", "system", str(exc)[:300])

        pdf_path = self.cfg.out_dir / f"{rfq_id}.pdf"
        ok, reason = False, "PDF отключён настройкой"
        if self.cfg.pdf_enabled and _pdf_works is not False:
            ok, reason = html_to_pdf(html_path, pdf_path)
            _pdf_works = ok
        if ok:
            self.store.add_artifact(rfq_id, "kp_pdf", pdf_path, sha256_bytes(pdf_path.read_bytes()))
            result["pdf"] = pdf_path
        else:
            log.warning("PDF не создан: %s", reason)
            self.store.log(rfq_id, "pdf_skipped", "system", reason[:300])
            result["pdf_error"] = reason
        return result

    # ---------- решение человека ----------

    def approve(self, rfq_id: str, *, actor: str, comment: str = "") -> IngestResult:
        rfq = self.store.get_rfq(rfq_id)
        if rfq is None:
            raise KeyError(rfq_id)
        quote = self.store.get_quote(rfq_id) or {}
        self.store.log(rfq_id, "approved", actor, comment)

        reply = build_reply_text_from_store(rfq, quote, rfq_id)
        sent_path = self._write_outbox(rfq_id, rfq, reply)
        self.store.add_artifact(rfq_id, "reply_eml", sent_path, sha256_bytes(sent_path.read_bytes()))
        self.store.set_status(rfq_id, RfqStatus.SENT.value)
        self.store.log(rfq_id, "sent", actor, "черновик сохранён в outbox (SMTP в прототипе выключен)")

        return IngestResult(
            rfq_id=rfq_id,
            status=RfqStatus.SENT.value,
            lines=len(rfq.get("lines", [])),
            matched=len(quote.get("lines", [])),
            total=str(quote.get("total", "0.00")),
            html_path=str(self.cfg.out_dir / f"{rfq_id}.html"),
            docx_path=str(self.cfg.out_dir / f"{rfq_id}.docx"),
            pdf_path=str(self.cfg.out_dir / f"{rfq_id}.pdf"),
            reply_text=reply,
        )

    def reject(self, rfq_id: str, *, actor: str, reason: str = "") -> None:
        if self.store.get_rfq(rfq_id) is None:
            raise KeyError(rfq_id)
        self.store.set_status(rfq_id, RfqStatus.REJECTED.value)
        self.store.log(rfq_id, "rejected", actor, reason)

    def _write_outbox(self, rfq_id: str, rfq: dict, body: str) -> Path:
        from .mailparse import build_reply_eml

        raw = build_reply_eml(
            sender_to=rfq["sender"],
            sender_from=self.cfg.company_email,
            subject=build_reply_subject(rfq["subject"]),
            body=body,
            in_reply_to=rfq.get("message_id"),
        )
        path = self.cfg.out_dir / "sent" / f"{rfq_id}.eml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        return path


def _money_text(value: object) -> str:
    amount = Decimal(str(value or "0"))
    return f"{amount:,.2f}".replace(",", " ").replace(".", ",")


def build_reply_text(parsed: ParsedEmail, quote: Quote, rfq_id: str) -> str:
    head = [
        "Здравствуйте!",
        "",
        "Спасибо за заявку. Подготовили коммерческое предложение — во вложении.",
        "",
        f"Позиций в расчёте: {len(quote.lines)}",
    ]
    if quote.unmatched:
        head.append(
            f"Не вошли в расчёт (нужно уточнить): {quote.unmatched} — мы свяжемся отдельно."
        )
    head += [
        f"Сумма без НДС: {_money_text(quote.subtotal)} ₽",
        f"НДС: {_money_text(quote.vat)} ₽",
        f"Итого: {_money_text(quote.total)} ₽",
        "",
        "Предложение действительно 7 дней.",
        "Если всё подходит — ответьте на это письмо, и мы выставим счёт.",
        "",
        "С уважением,",
        "отдел продаж",
    ]
    return "\n".join(head)


def build_reply_text_from_store(rfq: dict, quote: dict, rfq_id: str) -> str:
    lines_count = len(quote.get("lines", []))
    unmatched = int(quote.get("unmatched", 0) or 0)
    head = [
        "Здравствуйте!",
        "",
        "Спасибо за заявку. Подготовили коммерческое предложение — во вложении.",
        "",
        f"Позиций в расчёте: {lines_count}",
    ]
    if unmatched:
        head.append(f"Не вошли в расчёт (нужно уточнить): {unmatched} — мы свяжемся отдельно.")
    head += [
        f"Сумма без НДС: {_money_text(quote.get('subtotal'))} ₽",
        f"НДС: {_money_text(quote.get('vat'))} ₽",
        f"Итого: {_money_text(quote.get('total'))} ₽",
        "",
        "Предложение действительно 7 дней.",
        "Если всё подходит — ответьте на это письмо, и мы выставим счёт.",
        "",
        "С уважением,",
        "отдел продаж",
    ]
    return "\n".join(head)