# PyInstaller spec — KittyMind Python server
# Usage:
#   uv run pyinstaller build-python.spec --distpath dist-python

block_cipher = None

a = Analysis(
    ['server/app.py'],
    pathex=['.'],
    binaries=[],
    datas=[],
    hiddenimports=[
        # kittymind subpackages (PyInstaller may miss them without explicit listing)
        'kittymind', 'kittymind.core', 'kittymind.agent', 'kittymind.tools',
        'kittymind.tools.builtin', 'kittymind.events', 'kittymind.session',
        'kittymind.memory', 'kittymind.callbacks',
        # server modules
        'server', 'server.ws_server', 'server.rpc_handler',
        # websockets (v12+ uses importlib-based subpackage resolution)
        'websockets', 'websockets.legacy', 'websockets.legacy.server',
        'websockets.legacy.protocol', 'websockets.legacy.client',
        'websockets.asyncio', 'websockets.asyncio.server',
        'websockets.asyncio.connection', 'websockets.asyncio.client',
        # pydantic v2 uses compiled extension modules
        'pydantic', 'pydantic.v1', 'pydantic_core',
        # openai
        'openai', 'openai.types', 'openai.resources',
        # misc
        'dotenv', 'mss', 'mss.windows', 'pyperclip',
        'tzdata', 'zoneinfo', 'zoneinfo._tzpath',
        'httpx', 'httpcore', 'anyio', 'sniffio',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'matplotlib', 'scipy', 'PIL', 'cv2'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='server',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,        # skip UPX: faster build, avoids AV false positives
    console=True,     # MUST be True: Electron reads stdout for [ready] signal
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='server',
)
