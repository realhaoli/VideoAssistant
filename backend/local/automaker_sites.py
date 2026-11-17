"""Chinese-market automaker websites eligible for homepage video discovery."""

AUTOMAKER_DOMAINS = frozenset('''
    byd.com chery.cn cheryinternational.com exeedcars.com jetour.com.cn icar-team.com
    xiaopeng.com lixiang.com xiaomiev.com tesla.cn tesla.com leapmotor.cn leapmotor.com
    hima.auto aito.auto seres.cn nio.cn nio.com onvo.cn firefly.com.cn
    changan.com.cn globalchangan.com deepal.com.cn avatr.com ford.com.cn changan-mazda.com.cn
    gwm.com.cn haval.com.cn wey.com tanksuv.com oraev.com
    geely.com geelyauto.com.cn zeekrlife.com zeekrgroup.com lynkco.com.cn
    gacgroup.com gacmotor.com aion.com.cn hyptec.com ghac.cn gac-toyota.com.cn
    dfmc.com.cn yipai.com.cn dfpv.com.cn voyah.com.cn hero-sales.com.cn dongfeng-honda.com
    dongfeng-nissan.com.cn peugeot.com.cn dongfeng-citroen.com.cn dpca.com.cn
    saicmotor.com roewe.com.cn saicmg.com immotors.com risingauto.com wuling.com sgmw.com.cn csvw.com svw-volkswagen.com
    faw.com.cn faw.cn faw-vw.com ftms.com.cn toyota.com.cn vw.com.cn honda.com.cn hongqi-auto.com
    baicgroup.com.cn baicmotor.com beijingauto.com.cn arcfox.com.cn arcfox.cn mercedes-benz.com.cn beijing-hyundai.com.cn
    jac.com.cn brillianceauto.com bmw.com.cn haima.com
'''.split())


def automaker_domain(host):
    if host:
        while host:
            if host in AUTOMAKER_DOMAINS:
                return host
            host = host.partition('.')[2]
    return None


def is_root(path):
    return path.lower().rstrip('/') in {'', '/cn', '/zh', '/zh-cn', '/index.html', '/index.shtml', '/homepage'}


HOME_PAGES = {
    'haima.com': ('/hm/index.html',),
    'avatr.com': ('/video', '/07'),
    'changan.com.cn': ('https://www.globalchangan.com/cn/',),
    'dfmc.com.cn': ('/news/shipinzhuanqu.html',),
    'baicgroup.com.cn': ('/baicgroup/News/Video/A160003002Gone1.html', 'https://www.arcfox.com.cn/'),
    'gacgroup.com': ('https://www.gacmotor.com/', 'https://www.aion.com.cn/', 'https://www.hyptec.com/'),
    'saicmotor.com': ('https://www.sgmw.com.cn/', 'https://www.saicmg.com/', 'https://www.roewe.com.cn/'),
    'faw.com.cn': ('https://hongqi.faw.cn/', 'https://benteng.faw.cn/'),
    'jac.com.cn': ('https://yiwei.jac.com.cn/', 'https://passengercar.jac.com.cn/'),
    'zeekrlife.com': ('/zeekr001fr',),
    'csvw.com': ('https://brand.svw-volkswagen.com/brandHistory/wonderfulVideo',),
    'leapmotor.cn': ('/D19.html', '/B01.html'),
    'leapmotor.com': ('https://leapmotor.cn/D19.html',),
    'yipai.com.cn': ('/brandindex', '/nami01'),
}

DYNAMIC_HOME_PAGES = {
    'leapmotor.cn': 'https://www.leapmotor.cn/D19.html',
    'leapmotor.com': 'https://www.leapmotor.cn/D19.html',
}
