from plugin import create_plugin_instance

setting = {
    "filepath": __file__,
    "use_db": True,
    "use_default_setting": True,
    "home_module": "list",
    "menu": {
        "uri": __package__,
        "name": "KK Radio",
        "list": [
            {"uri": "list", "name": "채널"},
            {"uri": "ymplayer", "name": "YMPlayer용"},
            {"uri": "setting", "name": "설정"},
            {"uri": "log", "name": "로그"},
        ],
    },
    "setting_menu": None,
    "default_route": "single",
}

P = create_plugin_instance(setting)
from .logic import Logic
P.set_module_list([Logic])
