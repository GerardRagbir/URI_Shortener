"""NiceGUI pages: shorten, sign in, link list, link analytics and user administration."""
import html
import ipaddress
import math

from fastapi import Request
from fastapi.responses import RedirectResponse
from nicegui import ui

from ..api import authenticate
from ..config import get_settings
from ..db import DuplicateError, Role
from ..security import hash_password
from ..services.analytics import Summary, summarize
from ..services.guard import DAY, HOUR, Denied, ip_key
from ..services.shortener import InvalidAliasError, InvalidUrlError, create_link
from ..state import client_ip, public_origin, state
from .common import PANEL, PRIMARY, current_user, frame, link_card, safe_next, sign_in, sign_out, time_ago

settings = get_settings()


def login_redirect(request: Request) -> RedirectResponse:
    return RedirectResponse(f'/login?next={request.url.path}')


# --------------------------------------------------------------------------- shorten

@ui.page('/')
async def home_page(request: Request):
    user = await current_user()
    origin = public_origin(request)
    may_shorten = bool(user) or settings.allow_anonymous

    ip = client_ip(request)

    async def shorten() -> None:
        url_input.error = alias_input.error = None
        try:
            await state.guard.check('create', ip)
            link = await create_link(
                state.db, url_input.value or '',
                owner_id=user.id if user else None,
                alias=alias_input.value or None,
            )
        except (InvalidUrlError, Denied) as e:
            url_input.error = str(e)
            return
        except InvalidAliasError as e:
            alias_input.error = str(e)
            return
        result.clear()
        with result, ui.card().classes('w-full p-5'):
            link_card(link, origin, show_stats_button=bool(user))
            if not user:
                ui.label('Sign in before shortening to track clicks and see them on a map.') \
                    .classes('text-sm text-slate-500')

    with frame(user), ui.column().classes('w-full max-w-2xl mx-auto gap-5 py-8'):
        ui.label('Shorten a link').classes('text-3xl font-semibold')
        ui.label('Get a short URL and a QR code, then watch where the clicks come from.') \
            .classes('text-slate-600 -mt-3')
        with ui.card().classes('w-full p-5 gap-3'):
            url_input = ui.input('Long URL', placeholder='https://example.com/a/very/long/address') \
                .props('outlined clearable autofocus type=url').classes('w-full').on('keydown.enter', shorten)
            with ui.row().classes('w-full items-start gap-3'):
                alias_input = ui.input('Custom alias (optional)', placeholder='my-link') \
                    .props(f'outlined dense prefix="{origin.split("://", 1)[-1]}/"').classes('grow') \
                    .on('keydown.enter', shorten)
                ui.button('Shorten', icon='bolt', on_click=shorten).props('unelevated no-caps').classes('h-10 px-5')
            if not may_shorten:
                url_input.disable()
                alias_input.disable()
                ui.label('Sign in to shorten links.').classes('text-sm text-slate-500')
        result = ui.column().classes('w-full')


# --------------------------------------------------------------------------- session

@ui.page('/login')
async def login_page(request: Request, next: str = ''):  # noqa: A002 - query parameter name
    if await current_user():
        return RedirectResponse(safe_next(next))
    ip = client_ip(request)

    async def submit() -> None:
        try:
            await state.guard.check('login', ip, count=False)
        except Denied as e:
            password.error = str(e)
            return
        user = await authenticate(state.db, username.value.strip(), password.value)
        if not user:
            await state.guard.record('login', ip)
            password.error = 'Wrong username or password.'
            return
        sign_in(user)
        ui.navigate.to(safe_next(next))

    with frame(None), ui.card().classes('w-full max-w-sm mx-auto mt-12 p-6 gap-4'):
        ui.label('Sign in').classes('text-2xl font-semibold')
        username = ui.input('Username').props('outlined autofocus').classes('w-full').on('keydown.enter', submit)
        password = ui.input('Password', password=True, password_toggle_button=True) \
            .props('outlined').classes('w-full').on('keydown.enter', submit)
        ui.button('Sign in', on_click=submit).props('unelevated no-caps').classes('w-full')
    return None


@ui.page('/logout')
def logout_page():
    sign_out()
    return RedirectResponse('/')


# --------------------------------------------------------------------------- link list

@ui.page('/links')
async def links_page(request: Request):
    user = await current_user()
    if not user:
        return login_redirect(request)
    origin = public_origin(request)
    usernames = {u.id: u.username for u in await state.db.users.list()} if user.is_admin else {}

    async def load_rows() -> list[dict]:
        links = await state.db.links.list(None if user.is_admin else user.id)
        counts = await state.db.clicks.count_by_link()
        return [{
            'code': link.code,
            'short': f'{origin.split("://", 1)[-1]}/{link.code}',
            'target': link.target_url,
            'clicks': counts.get(link.id, 0),
            'created': link.created_at.strftime('%Y-%m-%d'),
            'owner': usernames.get(link.owner_id, '—'),
        } for link in links]

    async def confirm_delete(e) -> None:
        code = e.args['code']
        with ui.dialog() as dialog, ui.card().classes('p-5'):
            ui.label(f'Delete /{code}?').classes('text-lg font-semibold')
            ui.label('The short link stops working and its click history is removed.')
            with ui.row().classes('w-full justify-end'):
                ui.button('Cancel', on_click=lambda: dialog.submit(False)).props('flat no-caps')
                ui.button('Delete', color='negative', on_click=lambda: dialog.submit(True)).props('unelevated no-caps')
        if not await dialog:
            return
        link = await state.db.links.get_by_code(code)
        if link and (user.is_admin or link.owner_id == user.id):
            await state.db.links.delete(link.id)
        table.rows = await load_rows()
        ui.notify(f'Deleted /{code}')

    columns = [
        {'name': 'short', 'label': 'Short link', 'field': 'short', 'align': 'left', 'sortable': True},
        {'name': 'target', 'label': 'Destination', 'field': 'target', 'align': 'left'},
        {'name': 'clicks', 'label': 'Clicks', 'field': 'clicks', 'sortable': True},
        {'name': 'created', 'label': 'Created', 'field': 'created', 'sortable': True},
    ]
    if user.is_admin:
        columns.append({'name': 'owner', 'label': 'Owner', 'field': 'owner', 'align': 'left', 'sortable': True})
    columns.append({'name': 'actions', 'label': '', 'field': 'code', 'align': 'right'})

    with frame(user), ui.column().classes('w-full max-w-5xl mx-auto gap-4 py-6'):
        with ui.row().classes('w-full items-center'):
            ui.label('All links' if user.is_admin else 'My links').classes('text-3xl font-semibold')
            ui.space()
            ui.button('New link', icon='add', on_click=lambda: ui.navigate.to('/')).props('unelevated no-caps')
        table = ui.table(columns=columns, rows=await load_rows(), row_key='code', pagination=15) \
            .classes('w-full').props('flat bordered no-data-label="No links yet. Shorten one to get started."')
        table.add_slot('body-cell-short', '''
            <q-td :props="props">
                <a :href="'/links/' + props.row.code" class="font-semibold">{{ props.row.short }}</a>
            </q-td>''')
        table.add_slot('body-cell-target', '''
            <q-td :props="props">
                <div class="truncate" style="max-width: 22rem" :title="props.row.target">{{ props.row.target }}</div>
            </q-td>''')
        table.add_slot('body-cell-actions', '''
            <q-td :props="props">
                <q-btn flat round dense icon="insights" :href="'/links/' + props.row.code" aria-label="Analytics">
                    <q-tooltip>Analytics</q-tooltip>
                </q-btn>
                <q-btn flat round dense icon="delete" aria-label="Delete"
                       @click="$parent.$emit('delete', props.row)">
                    <q-tooltip>Delete</q-tooltip>
                </q-btn>
            </q-td>''')
        table.on('delete', confirm_delete)
    return None


# --------------------------------------------------------------------------- analytics

def daily_chart(summary: Summary) -> None:
    ink, grid = '#52514e', '#e7e6e2'
    ui.echart({
        'grid': {'left': 4, 'right': 8, 'top': 12, 'bottom': 4, 'containLabel': True},
        'tooltip': {'trigger': 'axis', 'axisPointer': {'type': 'shadow'}},
        'xAxis': {
            'type': 'category',
            'data': [f'{day:%b} {day.day}' for day, _ in summary.daily],
            'axisTick': {'show': False},
            'axisLine': {'lineStyle': {'color': grid}},
            'axisLabel': {'color': ink, 'fontSize': 11},
        },
        'yAxis': {
            'type': 'value', 'minInterval': 1, 'splitNumber': 3,
            'axisLabel': {'color': ink, 'fontSize': 11},
            'splitLine': {'lineStyle': {'color': grid}},
        },
        'series': [{
            'name': 'Clicks', 'type': 'bar', 'data': [n for _, n in summary.daily],
            'barMaxWidth': 24, 'itemStyle': {'color': PRIMARY, 'borderRadius': [4, 4, 0, 0]},
        }],
    }).classes('w-full h-40')


def stat(label: str, value: str) -> None:
    with ui.column().classes('gap-0'):
        ui.label(value).classes('text-2xl font-semibold leading-tight')
        ui.label(label).classes('text-xs uppercase tracking-wide text-slate-500')


@ui.page('/links/{code}')
async def link_page(code: str, request: Request):
    user = await current_user()
    if not user:
        return login_redirect(request)
    link = await state.db.links.get_by_code(code)
    if not link or not (user.is_admin or link.owner_id == user.id):
        with frame(user), ui.column().classes('w-full items-center gap-3 pt-16'):
            ui.label('Link not found').classes('text-2xl font-semibold')
            ui.button('Back to links', on_click=lambda: ui.navigate.to('/links')).props('flat no-caps')
        return None

    origin = public_origin(request)
    clicks = await state.db.clicks.list_for_link(link.id)
    shown = {'signature': None}
    markers: list = []

    @ui.refreshable
    def overview(summary: Summary) -> None:
        with ui.row().classes('w-full gap-8'):
            stat('Clicks', f'{summary.total:,}')
            stat('Countries', f'{len(summary.countries):,}')
            stat('Last click', time_ago(summary.last_click) if summary.last_click else '—')
        ui.label('Clicks per day, last 14 days (UTC)').classes('text-sm font-medium text-slate-700 pt-2')
        daily_chart(summary)

    @ui.refreshable
    def breakdown(summary: Summary, recent: list) -> None:
        if not summary.total:
            ui.label('No clicks yet').classes('font-semibold')
            ui.label('Share the short link or scan the QR code. Clicks show up here within a few seconds.') \
                .classes('text-sm text-slate-600')
            return
        ui.label('Top countries').classes('font-semibold')
        if not summary.countries:
            ui.label('None of the clicks could be located yet.').classes('text-sm text-slate-600')
        top = summary.countries[0][1] if summary.countries else 1
        for country, n in summary.countries[:6]:
            with ui.column().classes('w-full gap-1'):
                with ui.row().classes('w-full justify-between text-sm'):
                    ui.label(country)
                    ui.label(f'{n:,}').classes('tabular-nums text-slate-600')
                ui.linear_progress(n / top, show_value=False, size='6px').props('rounded track-color=grey-3')
        if summary.located < summary.total:
            missing = summary.total - summary.located
            ui.label(f'{missing:,} of {summary.total:,} clicks have no location. Private addresses such as '
                     'localhost cannot be geolocated.').classes('text-xs text-slate-500')

        ui.separator().classes('my-1')
        ui.label('Recent clicks').classes('font-semibold')
        for click in recent:
            with ui.row().classes('w-full justify-between items-baseline text-sm gap-3 flex-nowrap'):
                place = ', '.join(dict.fromkeys(p for p in (click.city, click.country) if p))
                ui.label(place or 'Unknown location').classes('truncate')
                ui.label(time_ago(click.clicked_at)).classes('text-slate-500 shrink-0')

    def draw_markers(summary: Summary) -> None:
        for marker in markers:
            world.remove_layer(marker)
        markers.clear()
        with world:
            for place in reversed(summary.places):  # busiest last, so it is drawn on top
                marker = world.generic_layer(name='circleMarker', args=[
                    [place.latitude, place.longitude],
                    {'radius': min(6 + 3 * math.sqrt(place.clicks), 26), 'color': '#ffffff', 'weight': 2,
                     'fillColor': PRIMARY, 'fillOpacity': 0.85},
                ])
                noun = 'click' if place.clicks == 1 else 'clicks'
                marker.run_method('bindTooltip', f'<b>{html.escape(place.label)}</b><br>{place.clicks:,} {noun}')
                markers.append(marker)

    def fit_map(summary: Summary) -> None:
        if not summary.places:
            return
        points = [[p.latitude, p.longitude] for p in summary.places]
        # keep the markers clear of the floating panels, whose layout depends on the window width
        world.run_map_method(':fitBounds', str(points), '''{
            paddingTopLeft: [window.innerWidth >= 1100 ? 440 : 30, 80],
            paddingBottomRight: [window.innerWidth >= 1100 ? 360 : 30,
                                 window.innerWidth >= 1100 ? 30 : window.innerHeight * 0.5],
            maxZoom: 6,
        }''')

    async def poll() -> None:
        latest = await state.db.clicks.list_for_link(link.id)
        summary = summarize(latest)
        signature = (summary.total, summary.located)
        if signature == shown['signature']:
            return
        first = shown['signature'] is None
        grew = not first and summary.located > shown['signature'][1]
        shown['signature'] = signature
        draw_markers(summary)
        if first or grew:
            fit_map(summary)
        if not first:
            overview.refresh(summary)
            breakdown.refresh(summary, latest[:8])

    with frame(user, padded=False):
        # The map is the page background; everything else floats above it.
        world = ui.leaflet(center=(20, 0), zoom=2, options={
            'zoomControl': False, 'worldCopyJump': True, 'minZoom': 2, 'zoomSnap': 0.5,
        }).classes('fixed inset-0 w-screen h-screen z-0')

        summary = summarize(clicks)
        with ui.row().classes('relative z-10 w-full justify-between items-start gap-4 p-4 pointer-events-none '
                              'max-[1099px]:mt-[45vh]'):
            with ui.column().classes('w-[26rem] max-w-full gap-4 pointer-events-auto'):
                with ui.column().classes(f'{PANEL} w-full p-4'):
                    link_card(link, origin, show_stats_button=False)
                with ui.column().classes(f'{PANEL} w-full p-4 gap-2'):
                    overview(summary)
            with ui.column().classes(f'{PANEL} w-[20rem] max-w-full p-4 gap-2 pointer-events-auto'):
                breakdown(summary, clicks[:8])

    async def start() -> None:
        await world.initialized()
        world.run_map_method(':addControl', 'L.control.zoom({position: "bottomright"})')
        await poll()
        ui.timer(5.0, poll)

    ui.timer(0, start, once=True)
    return None


# --------------------------------------------------------------------------- users

@ui.page('/admin/users')
async def users_page(request: Request):
    user = await current_user()
    if not user:
        return login_redirect(request)
    if not user.is_admin:
        return RedirectResponse('/links')

    @ui.refreshable
    async def user_list() -> None:
        for account in await state.db.users.list():
            with ui.row().classes('w-full items-center gap-3 py-1'):
                ui.icon('shield' if account.is_admin else 'person').classes('text-xl text-slate-500')
                ui.label(account.username).classes('font-medium')
                ui.badge(account.role.value, color='primary' if account.is_admin else 'grey-6')
                ui.space()
                ui.label(f'since {account.created_at:%Y-%m-%d}').classes('text-sm text-slate-500 max-sm:hidden')
                delete = ui.button(icon='delete', on_click=lambda account=account: remove(account)) \
                    .props('flat round dense').tooltip('Delete user')
                if account.id == user.id:
                    delete.disable()
                    delete.tooltip('You cannot delete your own account')

    async def remove(account) -> None:
        with ui.dialog() as dialog, ui.card().classes('p-5'):
            ui.label(f'Delete user "{account.username}"?').classes('text-lg font-semibold')
            ui.label('Their links keep working but no longer belong to anyone.')
            with ui.row().classes('w-full justify-end'):
                ui.button('Cancel', on_click=lambda: dialog.submit(False)).props('flat no-caps')
                ui.button('Delete', color='negative', on_click=lambda: dialog.submit(True)).props('unelevated no-caps')
        if await dialog and account.id != user.id:
            await state.db.users.delete(account.id)
            user_list.refresh()

    async def add() -> None:
        username.error = password.error = None
        name = username.value.strip()
        if not 3 <= len(name) <= 64:
            username.error = 'Use 3 to 64 characters.'
            return
        if len(password.value) < 8:
            password.error = 'Use at least 8 characters.'
            return
        try:
            await state.db.users.add(name, hash_password(password.value), Role(role.value))
        except DuplicateError:
            username.error = 'That username is taken.'
            return
        username.value = password.value = ''
        ui.notify(f'Created {name}', type='positive')
        user_list.refresh()

    with frame(user), ui.column().classes('w-full max-w-2xl mx-auto gap-4 py-6'):
        ui.label('Users').classes('text-3xl font-semibold')
        with ui.card().classes('w-full p-5 gap-0'):
            await user_list()
        with ui.card().classes('w-full p-5 gap-3'):
            ui.label('Add a user').classes('text-lg font-semibold')
            with ui.row().classes('w-full items-start gap-3'):
                username = ui.input('Username').props('outlined dense').classes('grow')
                password = ui.input('Password', password=True, password_toggle_button=True) \
                    .props('outlined dense').classes('grow')
                role = ui.select({Role.USER.value: 'User', Role.ADMIN.value: 'Admin'}, value=Role.USER.value) \
                    .props('outlined dense').classes('w-28')
            ui.button('Add user', icon='person_add', on_click=add).props('unelevated no-caps')
    return None


# --------------------------------------------------------------------------- blocked addresses

BLOCK_DURATIONS = {HOUR: '1 hour', DAY: '24 hours', 7 * DAY: '7 days', 30 * DAY: '30 days', 0: 'Until unblocked'}


@ui.page('/admin/blocks')
async def blocks_page(request: Request):
    user = await current_user()
    if not user:
        return login_redirect(request)
    if not user.is_admin:
        return RedirectResponse('/links')
    own_address = ip_key(client_ip(request))

    @ui.refreshable
    def block_list() -> None:
        blocks = state.guard.blocks()
        if not blocks:
            ui.label('No addresses are blocked.').classes('text-slate-600')
        for block in blocks:
            with ui.row().classes('w-full items-center gap-3 py-1 flex-nowrap'):
                ui.icon('block').classes('text-xl text-slate-500')
                with ui.column().classes('gap-0 min-w-0 grow'):
                    ui.label(block.ip).classes('font-medium font-mono')
                    ui.label(block.reason).classes('text-sm text-slate-600')
                ui.label(f'until {block.expires_at:%Y-%m-%d %H:%M} UTC' if block.expires_at else 'until unblocked') \
                    .classes('text-sm text-slate-500 shrink-0 max-sm:hidden')
                ui.button(icon='lock_open', on_click=lambda block=block: unblock(block.ip)) \
                    .props('flat round dense').tooltip('Unblock')

    async def unblock(ip: str) -> None:
        await state.guard.unblock(ip)
        ui.notify(f'Unblocked {ip}')
        block_list.refresh()

    async def add() -> None:
        address.error = None
        key = ip_key(address.value)
        try:
            ipaddress.ip_address(key or '')
        except ValueError:
            address.error = 'Enter an IPv4 or IPv6 address.'
            return
        if key == own_address:
            address.error = 'That is your own address; blocking it would lock you out.'
            return
        await state.guard.block(key, reason.value.strip() or f'Blocked by {user.username}', duration.value or None)
        address.value = reason.value = ''
        ui.notify(f'Blocked {key}', type='positive')
        block_list.refresh()

    with frame(user), ui.column().classes('w-full max-w-2xl mx-auto gap-4 py-6'):
        ui.label('Blocked addresses').classes('text-3xl font-semibold')
        ui.label(f'An address that exceeds a rate limit {settings.auto_block_strikes} times within an hour is '
                 f'blocked automatically for {settings.auto_block_hours} hours. Blocked addresses get a 403 '
                 'for every page, API call and short link.').classes('text-slate-600 -mt-2')
        with ui.card().classes('w-full p-5 gap-0'):
            block_list()
        with ui.card().classes('w-full p-5 gap-3'):
            ui.label('Block an address').classes('text-lg font-semibold')
            with ui.row().classes('w-full items-start gap-3'):
                address = ui.input('IP address', placeholder='203.0.113.7').props('outlined dense').classes('grow')
                duration = ui.select(BLOCK_DURATIONS, value=DAY).props('outlined dense').classes('w-40')
            reason = ui.input('Reason (optional)').props('outlined dense').classes('w-full')
            ui.button('Block', icon='block', on_click=add).props('unelevated no-caps')
    return None
