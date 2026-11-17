"""Pieces shared by all pages: session handling, the page frame and the link card."""
from contextlib import contextmanager
from datetime import UTC, datetime

from nicegui import app, ui

from ..db import Link, User
from ..services import qr
from ..state import state

PRIMARY = '#2a78d6'
PANEL = 'bg-white/90 backdrop-blur shadow-md rounded-xl'


async def current_user() -> User | None:
    user_id = app.storage.user.get('user_id')
    return await state.db.users.get(user_id) if user_id else None


def sign_in(user: User) -> None:
    app.storage.user['user_id'] = user.id


def sign_out() -> None:
    app.storage.user.pop('user_id', None)


def safe_next(target: str | None) -> str:
    """Only follow same-site paths after login."""
    if target and target.startswith('/') and not target.startswith('//') and '\\' not in target:
        return target
    return '/links'


def time_ago(moment: datetime) -> str:
    seconds = int((datetime.now(UTC) - moment).total_seconds())
    if seconds < 60:
        return 'just now'
    for unit, size in (('day', 86400), ('hour', 3600), ('minute', 60)):
        if seconds >= size:
            n = seconds // size
            return f'{n} {unit}{"s" if n != 1 else ""} ago'
    return 'just now'


@contextmanager
def frame(user: User | None, *, padded: bool = True):
    """Header with navigation; page content goes inside the ``with`` block."""
    ui.colors(primary=PRIMARY)
    ui.query('body').classes('bg-slate-50')
    if not padded:
        ui.query('.nicegui-content').classes('p-0')

    # (label, target, icon)
    pages = [('Shorten', '/', 'add_link')]
    if user:
        pages.append(('All links' if user.is_admin else 'My links', '/links', 'list'))
        if user.is_admin:
            pages.append(('Users', '/admin/users', 'group'))
            pages.append(('Blocked IPs', '/admin/blocks', 'block'))
    session = ('Sign out', '/logout', 'logout') if user else ('Sign in', '/login', 'login')

    # Phones: the navigation lives in a drawer opened from a menu button.
    with ui.left_drawer(value=False, bordered=True).props('overlay behavior=mobile :width="260"') \
            .classes('p-0') as drawer, ui.list().classes('w-full'):
        if user:
            with ui.item().classes('py-4'):
                with ui.item_section().props('avatar'):
                    ui.icon('shield' if user.is_admin else 'person').classes('text-2xl text-slate-500')
                with ui.item_section():
                    ui.item_label(user.username).classes('font-semibold')
                    ui.item_label(user.role.value).props('caption')
            ui.separator()
        for index, (label, target, icon) in enumerate([*pages, session]):
            if index == len(pages):
                ui.separator()
            with ui.item(on_click=lambda target=target: ui.navigate.to(target)).props('clickable'):
                with ui.item_section().props('avatar'):
                    ui.icon(icon)
                with ui.item_section():
                    ui.item_label(label)

    def nav(label: str, target: str, icon: str) -> None:
        ui.button(label, icon=icon, on_click=lambda: ui.navigate.to(target)) \
            .props('flat no-caps color=white').classes('max-sm:hidden')

    # Wider screens: the same entries sit in the header. On phones only the menu
    # button (left) and the centred logo remain.
    with ui.header().classes('relative items-center gap-1 px-4 py-2 min-h-[52px]'):
        ui.button(icon='menu', on_click=drawer.toggle).props('flat round dense color=white aria-label="Menu"') \
            .classes('sm:hidden')
        ui.link('µrl', '/').classes('text-white text-2xl font-bold no-underline sm:mr-4 '
                                    'max-sm:absolute max-sm:left-1/2 max-sm:-translate-x-1/2')
        for entry in pages:
            nav(*entry)
        ui.space()
        if user:
            ui.label(user.username).classes('max-sm:hidden opacity-80')
        nav(*session)
    yield


def link_card(link: Link, origin: str, *, show_stats_button: bool) -> None:
    """Short URL, its QR code, and the actions on them."""
    short_url = f'{origin}/{link.code}'
    qr_path = f'/api/links/{link.code}/qr'

    with ui.row().classes('w-full items-start gap-5 flex-nowrap max-sm:flex-wrap'):
        ui.image(qr.svg_data_uri(short_url)).classes('w-36 h-36 shrink-0 rounded border border-slate-200') \
            .props(f'alt="QR code for {short_url}" no-spinner')
        with ui.column().classes('gap-2 min-w-0 grow'):
            ui.label('Short link').classes('text-xs uppercase tracking-wide text-slate-500')
            with ui.row().classes('items-center gap-1 flex-nowrap w-full'):
                ui.link(short_url.split('://', 1)[-1], short_url, new_tab=True) \
                    .classes('text-xl font-semibold truncate')

                async def copy() -> None:
                    await ui.clipboard.write(short_url)
                    ui.notify('Copied', type='positive', timeout=1500)
                ui.button(icon='content_copy', on_click=copy).props('flat round dense').tooltip('Copy short link')
            ui.label(link.target_url).classes('text-sm text-slate-600 break-all line-clamp-2')
            with ui.row().classes('gap-2 pt-1'):
                if show_stats_button:
                    ui.button('Analytics', icon='insights', on_click=lambda: ui.navigate.to(f'/links/{link.code}')) \
                        .props('unelevated no-caps dense').classes('px-3')
                for label, fmt in (('SVG', 'svg'), ('PNG', 'png')):
                    ui.button(label, icon='download',
                              on_click=lambda fmt=fmt: ui.download.from_url(f'{qr_path}?format={fmt}&download=true')) \
                        .props('outline no-caps dense').classes('px-3').tooltip(f'Download QR code as {label}')
