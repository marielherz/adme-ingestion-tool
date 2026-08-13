"""Test doubles for Streamlit page assertions.

Extensions added for the ingestion page (``app/pages/5_📄_Manifest.py``):

- ``columns(spec)`` returns a list of ``StreamlitContext`` instances so the
  page can do ``cols = st.columns(3); with cols[0]: ...``.  The page uses
  ``st.columns`` to render the legal-tag/ACL input row and the run-status
  metric row.
- ``status(label, expanded=...)`` returns a :class:`StreamlitStatusContext`
  that behaves as a context manager AND exposes ``update(label=, state=)``
  so the page's ``status_box.update(...)`` calls inside ``with status_box:``
  blocks are recorded.

Extensions added for the legal tags page (``app/pages/3_🏷️_Legal_Tags.py``):

- ``toggle(label, value=False, ...)`` returns the configured widget value
  (or ``value`` default) so the "Show only valid tags" toggle's bool flow
  is observable.
- ``selectbox(label, options, index=0, ...)`` returns the configured widget
  value or ``options[index]``; used both for the table-row selection and the
  create-form classification dropdowns.
- ``multiselect(label, options, default=None, ...)`` returns the configured
  widget value (list) or ``default``; used for country dropdowns.
- ``date_input(label, value=None, ...)`` returns the configured widget value
  or ``value``; used for the create-form expiration field.
- ``text_area(label, value="", ...)`` returns the configured widget value or
  ``value``; used for the description fields.

All five lookups consult ``self.widget_values[label]`` first, then fall back
to the page's supplied default. Existing tests keep working — these are
additive on top of the ``__getattr__`` no-op fallback.
"""

from __future__ import annotations

from collections.abc import Callable
from types import ModuleType
from typing import Any, Literal, NamedTuple


class StreamlitCall(NamedTuple):
    """Represent one recorded Streamlit API call."""

    name: str
    args: tuple[Any, ...]
    kwargs: dict[str, Any]


class StreamlitContext:
    """Simple context manager used for forms and spinners."""

    def __init__(
        self,
        recorder: StreamlitRecorder,
        name: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> None:
        self._recorder = recorder
        self._recorder.calls.append(
            StreamlitCall(name=name, args=args, kwargs=kwargs)
        )

    def __enter__(self) -> StreamlitRecorder:
        return self._recorder

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> Literal[False]:
        return False


class StreamlitStatusContext(StreamlitContext):
    """``st.status`` context manager with a recorded ``.update(...)``.

    The ingestion page's ``status_box.update(label=..., state=...)`` calls
    inside ``with status_box:`` get appended to the recorder under the
    name ``status_update`` AND tracked on the instance so a single test
    can assert "the final state was 'error'".
    """

    def __init__(
        self,
        recorder: StreamlitRecorder,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> None:
        super().__init__(recorder, "status", args, kwargs)
        self.updates: list[dict[str, Any]] = []

    def update(self, **kwargs: Any) -> None:
        """Record a status update without altering recorder semantics."""
        self.updates.append(kwargs)
        self._recorder.calls.append(
            StreamlitCall(name="status_update", args=(), kwargs=kwargs)
        )


class StreamlitPageMock:
    """Stand-in for the object returned by ``st.Page``.

    Holds the page target and metadata.  ``run()`` invokes the target when
    it's a callable (the home page in ``app.main``); path-based pages are
    not executed here because page tests load those scripts directly.
    """

    def __init__(self, page: Any, kwargs: dict[str, Any]) -> None:
        self.page = page
        self.kwargs = kwargs
        self.title = kwargs.get("title")
        self.icon = kwargs.get("icon")
        self.default = bool(kwargs.get("default", False))

    def run(self) -> None:
        if callable(self.page):
            self.page()


class StreamlitNavigationMock:
    """Stand-in for the object returned by ``st.navigation``.

    ``run()`` invokes the default page (or first available) so ``main()``
    tests still observe the home content's recorded calls.
    """

    def __init__(self, recorder: StreamlitRecorder, pages: Any) -> None:
        self._recorder = recorder
        self.pages = pages
        if isinstance(pages, dict):
            self.flat_pages: list[StreamlitPageMock] = [
                p for plist in pages.values() for p in plist
            ]
        else:
            self.flat_pages = list(pages)

    def run(self) -> None:
        for page in self.flat_pages:
            if isinstance(page, StreamlitPageMock) and page.default:
                page.run()
                return
        if self.flat_pages and isinstance(self.flat_pages[0], StreamlitPageMock):
            self.flat_pages[0].run()


class StreamlitProgressMock:
    """Stand-in for the object returned by ``st.progress(...)``.

    The page calls ``bar = st.progress(0.0, text=...); bar.progress(0.5)``
    to update progress during submit loops. This mock records each
    ``.progress()`` update so tests can assert on final progress values.
    """

    def __init__(
        self,
        recorder: StreamlitRecorder,
        initial: float,
        kwargs: dict[str, Any],
    ) -> None:
        self._recorder = recorder
        self._recorder.calls.append(
            StreamlitCall(name="progress", args=(initial,), kwargs=kwargs)
        )
        self.updates: list[tuple[float, dict[str, Any]]] = []

    def progress(self, value: float, **kwargs: Any) -> None:
        """Record a progress-bar update."""
        self.updates.append((value, kwargs))
        self._recorder.calls.append(
            StreamlitCall(name="progress_update", args=(value,), kwargs=kwargs)
        )


class QueryParamsRecorder(dict[str, object]):
    """Record query-param clearing while behaving like Streamlit query params."""

    def __init__(self) -> None:
        super().__init__()
        self.clear_count = 0

    def clear(self) -> None:
        """Clear all query params and record that a clear happened."""
        self.clear_count += 1
        super().clear()


class StreamlitRecorder(ModuleType):
    """Record Streamlit API calls for page-level tests."""

    def __init__(self) -> None:
        super().__init__("streamlit")
        self.calls: list[StreamlitCall] = []
        self.session_state: dict[str, object] = {}
        self.query_params = QueryParamsRecorder()
        self.widget_values: dict[str, Any] = {}
        self.submit_responses: dict[str, bool] = {}
        self.button_responses: dict[str, bool] = {}
        self.file_uploader_responses: dict[str, Any] = {}

    def form(self, key: str, **kwargs: Any) -> StreamlitContext:
        """Return a context manager for a Streamlit form."""
        return StreamlitContext(self, "form", (key,), kwargs)

    def spinner(self, text: str, **kwargs: Any) -> StreamlitContext:
        """Return a context manager for a Streamlit spinner."""
        return StreamlitContext(self, "spinner", (text,), kwargs)

    def expander(self, label: str, **kwargs: Any) -> StreamlitContext:
        """Return a context manager for a Streamlit expander."""
        return StreamlitContext(self, "expander", (label,), kwargs)

    def columns(self, spec: int | list[int], **kwargs: Any) -> list[StreamlitContext]:
        """Return ``N`` context managers for ``with cols[i]:`` blocks.

        ``spec`` may be an int column count or a list of relative widths.
        Each returned context records its own ``column`` call when entered
        so we can still assert "the page rendered three columns" without
        coupling tests to the underlying widget order.
        """
        count = spec if isinstance(spec, int) else len(spec)
        self.calls.append(
            StreamlitCall(name="columns", args=(spec,), kwargs=kwargs)
        )
        return [
            StreamlitContext(self, "column", (index,), {})
            for index in range(count)
        ]

    def tabs(self, labels: list[str], **kwargs: Any) -> list[StreamlitContext]:
        """Return context managers for ``tab_a, tab_b = st.tabs([...])``."""
        self.calls.append(
            StreamlitCall(name="tabs", args=(labels,), kwargs=kwargs)
        )
        return [
            StreamlitContext(self, "tab", (label,), {})
            for label in labels
        ]

    def progress(self, value: float = 0.0, **kwargs: Any) -> StreamlitProgressMock:
        """Return a progress-bar mock with a chainable ``.progress()`` method."""
        return StreamlitProgressMock(self, value, kwargs)

    def status(self, label: str, **kwargs: Any) -> StreamlitStatusContext:
        """Return a context manager that also exposes ``.update(...)``."""
        return StreamlitStatusContext(self, (label,), kwargs)

    def tabs(self, labels: list[str], **kwargs: Any) -> list[StreamlitContext]:
        """Return one ``StreamlitContext`` per tab label.

        Mirrors :meth:`columns`: records the call once, then returns
        independent context managers so pages can do ``with tabs[i]:``.
        """
        self.calls.append(
            StreamlitCall(name="tabs", args=(labels,), kwargs=kwargs)
        )
        return [
            StreamlitContext(self, "tab", (label,), {}) for label in labels
        ]

    def number_input(
        self,
        label: str,
        min_value: Any = None,
        max_value: Any = None,
        value: Any = None,
        **kwargs: Any,
    ) -> Any:
        """Record a number_input and return the configured / default value."""
        self.calls.append(
            StreamlitCall(
                name="number_input",
                args=(label,),
                kwargs={
                    "min_value": min_value,
                    "max_value": max_value,
                    "value": value,
                    **kwargs,
                },
            )
        )
        return self.widget_values.get(label, value)

    def checkbox(
        self, label: str, value: bool = False, **kwargs: Any
    ) -> bool:
        """Record a checkbox and return the configured value (bool)."""
        self.calls.append(
            StreamlitCall(
                name="checkbox",
                args=(label,),
                kwargs={"value": value, **kwargs},
            )
        )
        return bool(self.widget_values.get(label, value))

    def text_input(self, label: str, value: str = "", **kwargs: Any) -> str:
        """Record a text input and return the configured widget value.

        Resolution order mirrors Streamlit's precedence for keyed widgets:
        an explicit ``widget_values[label]`` test override wins first, then a
        seeded ``session_state[key]`` value (for widgets that manage their value
        through ``key`` instead of ``value=``), and finally the ``value``
        default.
        """
        self.calls.append(
            StreamlitCall(
                name="text_input",
                args=(label,),
                kwargs={"value": value, **kwargs},
            )
        )
        if label in self.widget_values:
            return str(self.widget_values[label])
        key = kwargs.get("key")
        if key is not None and key in self.session_state:
            return str(self.session_state[key])
        return str(value)

    def radio(
        self,
        label: str,
        options: list[Any],
        index: int = 0,
        **kwargs: Any,
    ) -> Any:
        """Record a radio input and return the configured widget value."""
        self.calls.append(
            StreamlitCall(
                name="radio",
                args=(label, options),
                kwargs={"index": index, **kwargs},
            )
        )
        return self.widget_values.get(label, options[index])

    def form_submit_button(self, label: str, **kwargs: Any) -> bool:
        """Record a form submit button and return its configured response."""
        self.calls.append(
            StreamlitCall(
                name="form_submit_button",
                args=(label,),
                kwargs=kwargs,
            )
        )
        if kwargs.get("disabled"):
            return False
        return self.submit_responses.get(label, False)

    def button(self, label: str, **kwargs: Any) -> bool:
        """Record a button and return its configured response."""
        self.calls.append(
            StreamlitCall(
                name="button",
                args=(label,),
                kwargs=kwargs,
            )
        )
        if kwargs.get("disabled"):
            return False
        return self.button_responses.get(label, False)

    def checkbox(
        self, label: str, value: bool = False, **kwargs: Any
    ) -> bool:
        """Record a checkbox and return the configured widget value (bool).

        Mirrors a keyed Streamlit widget: a ``widget_values[label]`` override
        wins, else a seeded ``session_state[key]`` value, else ``value``. The
        resolved value is written back to ``session_state[key]``.
        """
        self.calls.append(
            StreamlitCall(
                name="checkbox",
                args=(label,),
                kwargs={"value": value, **kwargs},
            )
        )
        key = kwargs.get("key")
        if label in self.widget_values:
            resolved = bool(self.widget_values[label])
        elif key and key in self.session_state:
            resolved = bool(self.session_state[key])
        else:
            resolved = bool(value)
        if key:
            self.session_state[key] = resolved
        return resolved

    def toggle(
        self, label: str, value: bool = False, **kwargs: Any
    ) -> bool:
        """Record a toggle and return the configured widget value (bool)."""
        self.calls.append(
            StreamlitCall(
                name="toggle",
                args=(label,),
                kwargs={"value": value, **kwargs},
            )
        )
        return bool(self.widget_values.get(label, value))

    def selectbox(
        self,
        label: str,
        options: list[Any],
        index: int = 0,
        **kwargs: Any,
    ) -> Any:
        """Record a selectbox and return the configured value or options[index]."""
        self.calls.append(
            StreamlitCall(
                name="selectbox",
                args=(label, options),
                kwargs={"index": index, **kwargs},
            )
        )
        if label in self.widget_values:
            return self.widget_values[label]
        # Honour session-state key binding (mirrors real Streamlit behaviour).
        key = kwargs.get("key")
        if key and key in self.session_state:
            val = self.session_state[key]
            if val in (list(options) if options else []):
                return val
        if not options:
            return None
        try:
            return options[index]
        except (IndexError, TypeError):
            return options[0] if options else None

    def multiselect(
        self,
        label: str,
        options: list[Any],
        default: list[Any] | None = None,
        **kwargs: Any,
    ) -> list[Any]:
        """Record a multiselect and return the configured value (list).

        Mirrors a keyed Streamlit widget: a ``widget_values[label]`` override
        wins, else a seeded ``session_state[key]`` value, else ``default``. The
        resolved value is written back to ``session_state[key]``.
        """
        self.calls.append(
            StreamlitCall(
                name="multiselect",
                args=(label, options),
                kwargs={"default": default, **kwargs},
            )
        )
        key = kwargs.get("key")
        if label in self.widget_values:
            resolved = list(self.widget_values[label])
        elif key and key in self.session_state:
            resolved = list(self.session_state[key])
        else:
            resolved = list(default) if default else []
        if key:
            self.session_state[key] = resolved
        return resolved

    def date_input(
        self, label: str, value: Any = None, **kwargs: Any
    ) -> Any:
        """Record a date_input and return the configured value or default."""
        self.calls.append(
            StreamlitCall(
                name="date_input",
                args=(label,),
                kwargs={"value": value, **kwargs},
            )
        )
        return self.widget_values.get(label, value)

    def text_area(
        self, label: str, value: str = "", **kwargs: Any
    ) -> str:
        """Record a text_area and return the configured value or default."""
        self.calls.append(
            StreamlitCall(
                name="text_area",
                args=(label,),
                kwargs={"value": value, **kwargs},
            )
        )
        return str(self.widget_values.get(label, value))

    def file_uploader(
        self, label: str, **kwargs: Any
    ) -> Any:
        """Record a file_uploader and return a configurable mock value.

        Tests inject a fake "uploaded file" via
        ``recorder.file_uploader_responses[label]`` — typically an instance
        of :class:`FakeUploadedFile` or any object exposing ``name``,
        ``size``, ``type`` attrs and a ``getvalue()`` method.
        Returns ``None`` (no file selected) when nothing is registered.
        """
        self.calls.append(
            StreamlitCall(
                name="file_uploader",
                args=(label,),
                kwargs=kwargs,
            )
        )
        return self.file_uploader_responses.get(label)

    def Page(  # noqa: N802 (mirrors Streamlit's actual public name)
        self, page: Any, **kwargs: Any
    ) -> StreamlitPageMock:
        """Record an ``st.Page`` registration and return a runnable mock.

        The mock stores the page target (callable or path string) along with
        any ``title``/``icon``/``default`` kwargs so ``st.navigation(...).run()``
        can invoke the home page's callable during ``app.main.main()`` tests.
        """
        self.calls.append(
            StreamlitCall(name="Page", args=(page,), kwargs=kwargs)
        )
        return StreamlitPageMock(page, kwargs)

    def navigation(
        self,
        pages: Any,
        **kwargs: Any,
    ) -> StreamlitNavigationMock:
        """Record an ``st.navigation`` call and return a mock with ``.run()``.

        ``run()`` invokes the page marked ``default=True`` (or the first
        registered page) when its target is a callable; path-based pages are
        no-ops here because page-level tests load those scripts directly via
        ``importlib.util.spec_from_file_location``.
        """
        self.calls.append(
            StreamlitCall(name="navigation", args=(pages,), kwargs=kwargs)
        )
        return StreamlitNavigationMock(self, pages)

    def __getattr__(self, name: str) -> Callable[..., None]:
        """Return a recorder for any accessed Streamlit API function."""

        def _record(*args: Any, **kwargs: Any) -> None:
            self.calls.append(StreamlitCall(name=name, args=args, kwargs=kwargs))

        return _record

    def calls_named(self, name: str) -> list[StreamlitCall]:
        """Return all recorded calls with the given name."""
        return [call for call in self.calls if call.name == name]
