"""Пайплайн: письмо → позиции → матчинг → расчёт → КП → черновик ответа."""

from __future__ import annotations

import logging
import re
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .config import LEGAL_VAT_RATES, Settings, settings as default_settings
from .domain import (
    CalcSettings,
    LineStatus,
    Quote,
    RfqLine,
    RfqStatus,
    ValidationError,
    build_reply_subject,
    money,
    price_quote,
    status_for,
)
from .extract import RuleExtractor, trailing_quantity
from .llm import build_extractor, estimate_cost_rub
from .mailparse import ParsedEmail, parse_eml
from .matching import PriceIndex, match_lines
from .notify import Notifier
from .pricelist import default_price_items, load_price_items
from .render import html_to_pdf, render_quote_docx, render_template, write_html
from .store import Store, sha256_bytes

log = logging.getLogger("kpbot.pipeline")

# Предохранитель: если браузер в окружении не печатает, не долбим его на каждом письме.
# Но и не отключаем PDF навсегда: через PDF_RETRY_SECONDS пробуем снова, иначе один
# неудачный запуск Chrome лишал сервис PDF до перезапуска.
_pdf_works: bool | None = None
_pdf_failed_at: float = 0.0
PDF_RETRY_SECONDS = 600.0


def _pdf_allowed() -> bool:
    """Печатать ли PDF сейчас: после сбоя ждём PDF_RETRY_SECONDS."""
    if _pdf_works is not False:
        return True
    return (time.monotonic() - _pdf_failed_at) > PDF_RETRY_SECONDS


def _pdf_remember(ok: bool) -> None:
    global _pdf_works, _pdf_failed_at
    _pdf_works = ok
    _pdf_failed_at = 0.0 if ok else time.monotonic()


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
    # Сколько индексов прайса держим в памяти. Сервис обслуживает десятки клиентов:
    # без ограничения память растёт вместе с их числом, а пересборка индекса на
    # 10 000 позиций — это чтение базы и токенизация, около секунды.
    MAX_CACHED_INDEXES = 8

    def __init__(self, store: Store, cfg: Settings | None = None) -> None:
        self.store = store
        self.cfg = cfg or default_settings
        self.extractor = build_extractor(self.cfg)
        self.notifier = Notifier.from_settings(self.cfg)
        # Кэш индекса — строго на клиента. Общий кэш на всех означал бы, что один
        # поставщик считает по прайсу другого: одинаковые (число позиций, время
        # обновления) у копии базы давали один и тот же ключ.
        self._indexes: OrderedDict[str, tuple[tuple[int, str], PriceIndex]] = OrderedDict()

    def tenant_key(self) -> str:
        """Кто это: путь базы клиента. Ключ для всего, что живёт в памяти и файлах."""
        return str(getattr(self.store, "db_path", "") or "default")

    def out_dir(self) -> Path:
        """Папка файлов клиента: у каждого своя, чтобы документы не пересекались."""
        return tenant_out_dir(self.cfg, self.tenant_key())

    def price_index(self) -> PriceIndex:
        """Индекс прайса кэшируется по клиенту и пересобирается при изменении прайса."""
        tenant = self.tenant_key()
        key = (self.store.price_count(), self.store.price_updated_at())
        cached = self._indexes.get(tenant)
        if cached is not None and cached[0] == key:
            self._indexes.move_to_end(tenant)
            return cached[1]
        index = PriceIndex(self.store.price_items())
        self._indexes[tenant] = (key, index)
        self._indexes.move_to_end(tenant)
        while len(self._indexes) > self.MAX_CACHED_INDEXES:
            self._indexes.popitem(last=False)
        log.info("индекс прайса собран: %s позиций (%s)", len(index), Path(tenant).name)
        return index

    def calc(self) -> CalcSettings:
        """Ставка НДС, режим цен и доставка этого клиента.

        Сначала то, что задано в интерфейсе клиента, потом — значения из `.env`.
        У одного поставщика 22% и цены без НДС, у другого 10% и цены с НДС:
        общая настройка сервиса тут не годится.
        """
        return calc_settings(self.store, self.cfg)

    # ---------- прайс ----------

    def ensure_price_list(self) -> int:
        """Подхватывает data/price_list.xlsx|csv, а если их нет — пишет демо-прайс."""
        if self.store.price_count() > 0:
            return self.store.price_count()

        for candidate in (self.cfg.data_dir / "price_list.xlsx", self.cfg.data_dir / "price_list.csv"):
            if candidate.exists():
                try:
                    items = load_price_items(
                        candidate, prices_include_vat=self.cfg.prices_include_vat
                    )
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
        items = load_price_items(path, prices_include_vat=self.cfg.prices_include_vat)
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
        lines = self._apply_learned(lines, index)
        lines = self._drop_subsets_of_matched(lines)
        lines = self._dedupe_by_sku(lines)
        lines = self._cap_quantities(lines)
        quote = price_quote(
            lines,
            {item.sku: item for item in index.items},
            **self.calc().as_kwargs(max_qty=self.cfg.max_qty),
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
        started = time.perf_counter()
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
        self._record_llm_usage(rfq_id)

        html_path = self._render_documents(rfq_id, parsed, lines, quote)
        self.store.set_quote_duration(quote_id, int((time.perf_counter() - started) * 1000))
        reply_text = build_reply_text(
            parsed,
            quote,
            rfq_id,
            company=self.company(),
            valid_days=self.cfg.quote_valid_days,
        )

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
        """Строки без единицы, но с узнаваемой позицией прайса — человеку, не в мусор.

        Если клиент написал количество, но забыл единицу («Подрозетник 68 мм — 20»),
        строку не выбрасываем: количество сохраняем, единицу подставит прайс,
        а статус останется «нужно подтверждение».
        """
        promoted: list[RfqLine] = []
        for segment in skipped[:50]:
            item, score = index.best(segment)
            if item is None or score < 0.55:
                continue
            qty = trailing_quantity(segment)
            promoted.append(
                RfqLine(
                    idx=start + len(promoted),
                    raw_text=segment[:300],
                    qty=qty,
                    unit=None,
                    matched_sku=item.sku,
                    matched_name=item.name,
                    match_score=score,
                    status=LineStatus.INVALID,
                    note=(
                        "единица не указана — подставлена из прайса, нужно подтверждение"
                        if qty is not None
                        else "не указано количество или единица измерения"
                    ),
                )
            )
        return promoted

    def _record_llm_usage(self, rfq_id: str) -> None:
        """Сохраняет расход токенов и рублей на разбор этой заявки."""
        usage = getattr(self.extractor, "last_usage", None)
        if usage is None or usage.total <= 0:
            return
        cost = estimate_cost_rub(
            usage,
            self.cfg.llm_price_input_rub_per_1m,
            self.cfg.llm_price_output_rub_per_1m,
        )
        self.store.add_llm_usage(
            rfq_id,
            model=self.cfg.llm_model or "unknown",
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            cost_rub=str(cost),
            balance_rub=str(usage.balance_rub) if usage.balance_rub is not None else None,
        )

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
        detail = ", ".join(f"{channel}={'ok' if ok else 'fail'}" for channel, ok in sent.items())
        self.store.log(rfq_id, "notified" if any(sent.values()) else "notify_failed", "system", detail)

    @classmethod
    def learn_key(cls, raw_text: str) -> str:
        """Ключ обучения: название и размер без единиц, слова по алфавиту.

        Числа в ключе остаются: «кабель 3х2.5» и «кабель 3х2.25» — разные позиции,
        и обучать одну на другой нельзя.
        """
        words = re.findall(r"[0-9a-zа-яё][0-9a-zа-яё.]*", (raw_text or "").lower().replace("ё", "е"))
        cleaned = sorted({
            word for word in words
            if len(word) > 1 and not word.isdigit() and word not in cls.UNITS
        })
        return " ".join(cleaned)

    def _apply_learned(self, lines: list[RfqLine], index) -> list[RfqLine]:
        """Подставляет то, чему научились на правках менеджера."""
        items = {item.sku: item for item in index.items}
        for line in lines:
            if line.status == LineStatus.OK:
                continue
            key = self.learn_key(line.raw_text)
            if not key:
                continue
            sku = self.store.learned_sku(key)
            if not sku or sku not in items:
                continue
            item = items[sku]
            # Единица — такая же часть позиции, как артикул: «100 шт» по цене
            # за метр даёт ошибку в сто раз. Не совпало — отдаём человеку.
            if line.unit and item.unit and line.unit != item.unit:
                line.note = (
                    f"выученное совпадение не применимо: единица «{line.unit}» "
                    f"не совпадает с прайсом «{item.unit}»"
                )
                continue
            line.matched_sku = item.sku
            line.matched_name = item.name
            line.match_score = 1.0
            line.status = LineStatus.OK
            line.note = "выучено на правках"
        return lines

    def _learn_from_edits(self, before: dict[int, RfqLine], after: list[RfqLine]) -> None:
        """Учится на том, что человек подтвердил, заменил или удалил.

        Сравниваем по содержимому строки (ключ обучения + артикул), а не по
        номеру: после удаления строки из середины номера сдвигаются, и прежняя
        сверка «по idx» записывала подтверждение одному артикулу, а «промах» —
        другому, который на самом деле никто не трогал. Выученные пары от этого
        портились, и через несколько правок сервис уверенно подставлял не то.
        """
        before_pairs: set[tuple[str, str]] = set()
        before_status: dict[tuple[str, str], set[str]] = {}
        for line in before.values():
            if not line.matched_sku:
                continue
            key = self.learn_key(line.raw_text)
            if key:
                pair = (key, line.matched_sku)
                before_pairs.add(pair)
                before_status.setdefault(pair, set()).add(line.status.value)

        after_pairs: set[tuple[str, str]] = set()
        for line in after:
            key = self.learn_key(line.raw_text)
            if not key or not line.matched_sku:
                continue
            pair = (key, line.matched_sku)
            after_pairs.add(pair)
            # Новое соответствие — и подтверждение уже предложенного: человек
            # нажал «подтвердить» у похожей строки, и это такое же обучение.
            # Иначе пара «подтверждена дважды» не набиралась бы никогда: статус
            # после первого подтверждения уже ok, и второй правки не требуется.
            confirmed = (
                line.status is LineStatus.OK
                and LineStatus.AMBIGUOUS.value in before_status.get(pair, set())
            )
            if pair not in before_pairs or confirmed:
                self.store.learn_match(*pair)

        # Соответствие исчезло из заявки (строку удалили, заменили или сняли
        # совпадение) — это возражение против пары, а не против всей строки.
        for key, sku in sorted(before_pairs - after_pairs):
            self.store.learn_miss(key, sku)

    def _drop_subsets_of_matched(self, lines: list[RfqLine]) -> list[RfqLine]:
        """Выкидывает обрубки: строка без совпадения, чьи слова целиком входят
        в другую строку с тем же количеством и найденной позицией.

        Модель иногда дублирует «Шпаклевка финишная» отдельно от
        «Шпаклевка финишная 25 кг» — человеку такой мусор показывать незачем.
        """
        result: list[RfqLine] = []
        for line in lines:
            if line.matched_sku:
                result.append(line)
                continue
            words = self._name_words(line.raw_text)
            duplicate = False
            if words:
                for other in lines:
                    if other is line or not other.matched_sku:
                        continue
                    if line.qty is not None and other.qty != line.qty:
                        # Разное количество — это разные строки, а не обрубок.
                        continue
                    if words <= self._name_words(other.raw_text):
                        duplicate = True
                        break
            if not duplicate:
                result.append(line)
        return result

    UNITS = frozenset({"шт", "штук", "кг", "м", "м2", "м3", "меш", "мешок", "мешков", "уп", "упак",
                       "л", "литр", "компл", "рул", "рулон", "пар", "пач", "пачка", "км", "тонн", "т"})

    @classmethod
    def _name_words(cls, text: str) -> set[str]:
        """Слова названия без чисел и единиц: «Шпаклевка финишная — 10 меш» → {шпаклевка, финишная}."""
        words = re.split(r"[^0-9a-zа-яё]+", (text or "").lower())
        return {
            word for word in words
            if len(word) > 1 and not word.isdigit() and word not in cls.UNITS
        }

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

    def _cap_quantities(self, lines: list[RfqLine]) -> list[RfqLine]:
        """Строка с невероятным количеством — человеку, а не отказ всему письму.

        price_quote отвергает количество больше max_qty. Если не отсечь такую
        строку заранее, исключение уносит всё письмо: заявка не сохраняется,
        а через IMAP письмо уже помечено прочитанным — оно теряется целиком.
        """
        limit = self.cfg.max_qty
        for line in lines:
            if line.qty is not None and line.qty > limit:
                line.status = LineStatus.INVALID
                line.note = (
                    f"количество {line.qty} больше допустимого ({limit}) — проверьте строку"
                )
        return lines

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

        before = {line["idx"]: RfqLine(
            idx=line["idx"], raw_text=line["raw_text"], qty=Decimal(line["qty"]) if line["qty"] else None,
            unit=line["unit"], matched_sku=line["matched_sku"], match_score=line["score"],
            matched_name=line["matched_name"], status=LineStatus(line["status"]), note=line["note"],
        ) for line in rfq["lines"]}

        # Один SKU не может стоять в КП двумя строками: менеджер, добавляющий
        # позицию, которая уже есть, удвоил бы сумму молча.
        seen_sku: dict[str, RfqLine] = {}
        for line in lines:
            if line.status is LineStatus.OK and line.matched_sku:
                if line.matched_sku in seen_sku:
                    raise ValidationError(
                        f"позиция {line.matched_sku} уже есть в КП — "
                        "измените количество в существующей строке"
                    )
                seen_sku[line.matched_sku] = line

        for position, line in enumerate(lines, start=1):
            line.idx = position

        index = self.price_index()
        # Сначала расчёт, потом запись: если строка не проходит проверку,
        # в базе не останутся новые строки со старым КП.
        quote = price_quote(
            lines,
            {item.sku: item for item in index.items},
            **self.calc().as_kwargs(max_qty=self.cfg.max_qty),
        )
        self.store.replace_lines(rfq_id, lines)
        self._learn_from_edits(before, lines)
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

    def company(self) -> dict[str, str]:
        """Реквизиты для КП: сначала то, что задано в интерфейсе, потом .env."""
        saved = self.store.get_settings()
        defaults = {
            "name": self.cfg.company_name,
            "inn": self.cfg.company_inn,
            "kpp": self.cfg.company_kpp,
            "address": self.cfg.company_address,
            "phone": self.cfg.company_phone,
            "email": self.cfg.company_email,
            "signer": self.cfg.company_signer,
            "account": self.cfg.company_account,
            "bank": self.cfg.company_bank,
            "bik": self.cfg.company_bik,
            "corr_account": self.cfg.company_corr_account,
            "signature": "",
            "kp_intro": "",
            "logo": "",
            "color": "",
        }
        return {key: (saved.get(f"company_{key}") or value) for key, value in defaults.items()}

    def _render_documents(
        self, rfq_id: str, parsed: ParsedEmail, lines: list, quote: Quote
    ) -> dict[str, object]:
        company = self.company()
        client = {"sender": parsed.sender, "subject": parsed.subject}
        rfq_meta = {"id": rfq_id, "received_at": parsed.received_at}
        context = {
            "company": company,
            "client": client,
            "rfq": rfq_meta,
            "quote": quote,
            "lines": lines,
            "valid_days": self.cfg.quote_valid_days,
            "prices_include_vat": quote.prices_include_vat,
        }
        result: dict[str, object] = {"html": None, "docx": None, "pdf": None, "pdf_error": ""}

        try:
            html = render_template("kp.html", **context)
        except Exception as exc:  # шаблон не должен ронять приём письма
            log.exception("не удалось отрендерить КП")
            self.store.log(rfq_id, "render_error", "system", str(exc)[:500])
            return result

        html_path = write_html(self.out_dir() / f"{rfq_id}.html", html)
        self.store.add_artifact(rfq_id, "kp_html", html_path, sha256_bytes(html_path.read_bytes()))
        result["html"] = html_path

        try:
            docx_path = render_quote_docx(
                self.out_dir() / f"{rfq_id}.docx",
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

        pdf_path = self.out_dir() / f"{rfq_id}.pdf"
        ok, reason = False, "PDF отключён настройкой"
        if self.cfg.pdf_enabled and _pdf_allowed():
            ok, reason = html_to_pdf(html_path, pdf_path)
            # Сбой PDF — не приговор до перезапуска: раньше один неудачный вызов
            # (Chrome не успел стартовать) отключал PDF до рестарта сервиса.
            _pdf_remember(ok)
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
        current = RfqStatus(rfq["status"])
        if current in (RfqStatus.APPROVED, RfqStatus.SENT):
            # Повторное одобрение — это второй черновик и риск второго письма.
            log.warning("одобрение %s отклонено: статус уже %s", rfq_id, current.value)
            raise ValidationError(
                f"нельзя перевести заявку в «одобрена»: текущий статус — «{current.value}»"
            )
        quote = self.store.get_quote(rfq_id) or {}
        if not quote.get("lines"):
            # Пустое КП отправлять нечего: либо строки не найдены, либо всё
            # исключено. Одобрение такого расчёта создавало письмо без позиций.
            log.warning(
                "одобрение %s отклонено: в расчёте нет позиций (статус %s, unmatched=%s)",
                rfq_id, current.value, quote.get("unmatched"),
            )
            raise ValidationError(
                "в расчёте нет ни одной позиции — добавьте строки или отклоните заявку"
            )
        self.store.log(rfq_id, "approved", actor, comment)

        reply = build_reply_text_from_store(
            rfq,
            quote,
            rfq_id,
            self.company(),
            valid_days=self.cfg.quote_valid_days,
        )
        sent_path = self._write_outbox(rfq_id, rfq, reply)
        self.store.add_artifact(rfq_id, "reply_eml", sent_path, sha256_bytes(sent_path.read_bytes()))

        # «Отправлено» ставит только фактическая отправка (SMTP). Одобрение —
        # это «КП проверено и готово»: заявка остаётся редактируемой, а черновик
        # письма лежит в outbox. Раньше при выключенном SMTP одобрение ставило
        # sent, и заявка навсегда теряла возможность правки, хотя письмо не ушло.
        status = RfqStatus.APPROVED.value
        self.store.set_status(rfq_id, status)
        if self.cfg.smtp_enabled:
            self.store.log(rfq_id, "awaiting_send", actor, "одобрено, ждёт отправки по SMTP")
        else:
            self.store.log(
                rfq_id,
                "approved_draft",
                actor,
                "черновик ответа сохранён в outbox (SMTP не настроен)",
            )

        return IngestResult(
            rfq_id=rfq_id,
            status=status,
            lines=len(rfq.get("lines", [])),
            matched=len(quote.get("lines", [])),
            total=str(quote.get("total", "0.00")),
            html_path=str(self.out_dir() / f"{rfq_id}.html"),
            docx_path=str(self.out_dir() / f"{rfq_id}.docx"),
            pdf_path=str(self.out_dir() / f"{rfq_id}.pdf"),
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
        path = self.out_dir() / "sent" / f"{rfq_id}.eml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        return path


def _money_text(value: object) -> str:
    amount = Decimal(str(value or "0"))
    return f"{amount:,.2f}".replace(",", " ").replace(".", ",")


def tenant_out_dir(cfg, db_path: str | Path) -> Path:
    """Папка документов клиента.

    Основной клиент остаётся в прежней папке (совместимость с уже выпущенными КП),
    остальные — в `<data>/clients/<имя базы>/out`. Физическое разделение нужно не
    только для порядка: по нему видно, что один клиент не откроет документ другого,
    даже зная идентификатор заявки.
    """
    text = str(db_path or "").strip()
    if not text:
        return cfg.out_dir
    path = Path(text)
    try:
        if path.resolve() == Path(cfg.db_path).resolve():
            return cfg.out_dir
    except OSError:
        pass
    key = re.sub(r"[^A-Za-z0-9_.-]+", "_", path.stem)[:64] or "client"
    return cfg.data_dir / "clients" / key / "out"


def _money_value(value: object) -> Decimal:
    """Деньги из строки базы: битое значение считается нулём, письмо не падает."""
    try:
        return Decimal(str(value or "0"))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def calc_settings(store: Store, cfg) -> CalcSettings:
    """Как считать деньги у клиента: интерфейс важнее `.env`, мусор — тоже `.env`.

    Битое значение в базе не должно ронять приём писем и не должно менять деньги
    молча: пишем предупреждение и берём значение по умолчанию из настроек сервиса.
    """
    saved = store.get_settings()
    return CalcSettings(
        vat_rate=_rate_from(saved.get("vat_rate"), cfg.vat_rate),
        prices_include_vat=_bool_from(saved.get("prices_include_vat"), cfg.prices_include_vat),
        delivery_price=_money_from(saved.get("delivery_price"), cfg.delivery_price),
    )


def _rate_from(raw: object, fallback: Decimal) -> Decimal:
    # Запасное значение тоже должно быть законным: иначе неверная настройка
    # сервера «просочится» в документ через клиента без своей ставки.
    if fallback not in LEGAL_VAT_RATES:
        log.error(
            "ставка НДС по умолчанию %s не применяется с 01.01.2026 — беру 22%%",
            fallback,
        )
        fallback = Decimal("0.22")
    if raw is None or str(raw).strip() == "":
        return fallback
    try:
        value = Decimal(str(raw).strip().replace(",", "."))
    except (InvalidOperation, ValueError):
        log.warning("ставка НДС в настройках клиента не число (%r) — беру %s", raw, fallback)
        return fallback
    if not (0 <= value <= 1):
        log.warning(
            "ставка НДС в настройках клиента вне 0..1 (%s) — беру %s; "
            "в интерфейсе ставка задаётся долей: 22%% → 0.22",
            value,
            fallback,
        )
        return fallback
    if value not in LEGAL_VAT_RATES:
        # 20% и 18% — ставки прошлых лет. С 2026 года основная ставка 22%
        # (ст. 164 НК РФ); выпускать с ними документы нельзя, поэтому берём
        # законную ставку и громко пишем в журнал, а не «как настроено».
        log.error(
            "у клиента ставка НДС %s%% не применяется с 01.01.2026 — "
            "считаю по %s%%; исправьте настройку клиента",
            value * 100,
            fallback * 100,
        )
        return fallback
    return value


def _bool_from(raw: object, fallback: bool) -> bool:
    if raw is None or str(raw).strip() == "":
        return fallback
    return str(raw).strip().lower() in {"1", "true", "yes", "on", "да"}


def _money_from(raw: object, fallback: Decimal) -> Decimal:
    if raw is None or str(raw).strip() == "":
        return fallback
    try:
        value = Decimal(str(raw).strip().replace(",", "."))
    except (InvalidOperation, ValueError):
        log.warning("доставка в настройках клиента не число (%r) — беру %s", raw, fallback)
        return fallback
    if value < 0:
        log.warning("доставка в настройках клиента отрицательная (%s) — беру %s", value, fallback)
        return fallback
    return value


def _validity_phrase(days: int) -> str:
    """«7 календарных дней», «1 календарный день», «2 календарных дня»."""
    days = max(1, int(days or 1))
    if 11 <= days % 100 <= 14:
        return f"{days} календарных дней"
    if days % 10 == 1:
        return f"{days} календарный день"
    if 2 <= days % 10 <= 4:
        return f"{days} календарных дня"
    return f"{days} календарных дней"


def _reply_totals(
    subtotal: Decimal,
    vat: Decimal,
    delivery: Decimal,
    total: Decimal,
    *,
    prices_include_vat: bool,
    delivery_vat: Decimal = Decimal("0.00"),
) -> list[str]:
    """Строки итогов для письма так, чтобы они складывались в «Итого».

    В режиме «цены уже с НДС» доставка тоже уже с НДС: если показать её рядом
    с «Сумма без НДС» и «НДС», клиент сложит 1000 + 242 + 122 и получит 1364
    при «Итого 1342». Поэтому здесь либо доставка показывается без НДС
    (обычный режим), либо «Сумма с НДС по товарам» + «Доставка с НДС», а НДС
    идёт строкой «в том числе».
    """
    delivery = max(delivery, Decimal("0.00"))
    if prices_include_vat:
        goods_gross = money(subtotal + vat - delivery_vat)
        lines = [
            f"Сумма с НДС по товарам: {_money_text(goods_gross)} ₽",
            f"В том числе НДС: {_money_text(vat)} ₽",
        ]
        if delivery:
            lines.append(f"Доставка с НДС: {_money_text(delivery)} ₽")
    else:
        lines = [
            f"Сумма без НДС: {_money_text(subtotal)} ₽",
            f"НДС: {_money_text(vat)} ₽",
        ]
        if delivery:
            lines.append(f"Доставка без НДС: {_money_text(delivery)} ₽")
    lines.append(f"Итого: {_money_text(total)} ₽")
    return lines


def build_reply_text(
    parsed: ParsedEmail,
    quote: Quote,
    rfq_id: str,
    company: dict | None = None,
    *,
    valid_days: int = 7,
) -> str:
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
    head += _reply_totals(
        quote.subtotal,
        quote.vat,
        quote.delivery,
        quote.total,
        prices_include_vat=quote.prices_include_vat,
        delivery_vat=quote.delivery_vat,
    )
    head += [
        "",
        f"Предложение действительно {_validity_phrase(valid_days)}.",
        "Если всё подходит — ответьте на это письмо, и мы выставим счёт.",
        "",
        "С уважением,",
    ]
    signature = (company or {}).get("signature") or ""
    signer = (company or {}).get("signer") or ""
    name = (company or {}).get("name") or ""
    if signature:
        # в подписи можно использовать {{компания}}, {{подписант}}, {{телефон}}, {{почта}}
        filled = (
            signature.replace("{{компания}}", name)
            .replace("{{подписант}}", signer)
            .replace("{{телефон}}", (company or {}).get("phone") or "")
            .replace("{{почта}}", (company or {}).get("email") or "")
        )
        head.extend(filled.splitlines())
    else:
        head.append(signer or name or "отдел продаж")
    return "\n".join(head)


def build_reply_text_from_store(
    rfq: dict,
    quote: dict,
    rfq_id: str,
    company: dict | None = None,
    *,
    valid_days: int = 7,
) -> str:
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
    head += _reply_totals(
        _money_value(quote.get("subtotal")),
        _money_value(quote.get("vat")),
        _money_value(quote.get("delivery")),
        _money_value(quote.get("total")),
        prices_include_vat=str(quote.get("prices_include_vat") or "0").strip().lower()
        in {"1", "true", "yes", "on", "да"},
        delivery_vat=_money_value(quote.get("delivery_vat")),
    )
    head += [
        "",
        f"Предложение действительно {_validity_phrase(valid_days)}.",
        "Если всё подходит — ответьте на это письмо, и мы выставим счёт.",
        "",
        "С уважением,",
    ]
    signature = (company or {}).get("signature") or ""
    signer = (company or {}).get("signer") or ""
    name = (company or {}).get("name") or ""
    if signature:
        head.extend(
            signature.replace("{{компания}}", name)
            .replace("{{подписант}}", signer)
            .replace("{{телефон}}", (company or {}).get("phone") or "")
            .replace("{{почта}}", (company or {}).get("email") or "")
            .splitlines()
        )
    else:
        head.append(signer or name or "отдел продаж")
    return "\n".join(head)
