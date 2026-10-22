"""Which calendar days a run reports on, in the business time zone."""
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

REPORT_TZ = ZoneInfo("America/Los_Angeles")
BASELINE_DAYS = 7


@dataclass(frozen=True)
class Period:
    days: tuple            # reported days, oldest first
    baseline_days: tuple   # the 7 days immediately before the period
    run_date: date         # "today" for the priorities section

    @property
    def start(self) -> date:
        return self.days[0]

    @property
    def end(self) -> date:
        return self.days[-1]

    @property
    def window_start(self) -> date:
        """First day of data to fetch (start of the baseline)."""
        return self.baseline_days[0]

    @property
    def is_multi_day(self) -> bool:
        return len(self.days) > 1

    def label(self) -> str:
        if not self.is_multi_day:
            return self.start.strftime("%a, %b %-d, %Y")
        return f"{self.start.strftime('%a, %b %-d')} – {self.end.strftime('%a, %b %-d, %Y')}"

    def short_label(self) -> str:
        """Used in table headers: 'Yesterday' or 'Fri–Sun'."""
        if not self.is_multi_day:
            return "Yesterday" if self.start == self.run_date - timedelta(days=1) else self.start.strftime("%b %-d")
        return f"{self.start.strftime('%a')}–{self.end.strftime('%a')}"


def today_local() -> date:
    return datetime.now(REPORT_TZ).date()


def local_hour() -> int:
    return datetime.now(REPORT_TZ).hour


def build_period(run_date: date, report_date: date | None = None) -> Period:
    """A Monday run covers Fri, Sat and Sun so nothing from the weekend is
    skipped; any other run covers the previous day. An explicit report_date
    always covers just that day."""
    if report_date:
        days = [report_date]
        run_date = report_date + timedelta(days=1)
    elif run_date.weekday() == 0:
        days = [run_date - timedelta(days=n) for n in (3, 2, 1)]
    else:
        days = [run_date - timedelta(days=1)]
    baseline = [days[0] - timedelta(days=n) for n in range(BASELINE_DAYS, 0, -1)]
    return Period(days=tuple(days), baseline_days=tuple(baseline), run_date=run_date)


def local_day_bounds_utc(first: date, last: date) -> tuple[str, str]:
    """[start, end) of local days first..last as SOQL UTC datetime literals."""
    start = datetime.combine(first, time.min, REPORT_TZ).astimezone(timezone.utc)
    end = datetime.combine(last + timedelta(days=1), time.min, REPORT_TZ).astimezone(timezone.utc)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return start.strftime(fmt), end.strftime(fmt)


def utc_timestamp_to_local_date(value: str) -> date:
    """Salesforce datetimes look like '2026-10-15T21:04:11.000+0000'."""
    parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%f%z")
    return parsed.astimezone(REPORT_TZ).date()
