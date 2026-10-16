import requests
from prettytable import PrettyTable


def send_msg(content, receiver, auth, sender=None):
    url = 'http://xiaoluban.rnd.huawei.com:80/'
    data = {'content': content, 'receiver': receiver, 'auth': auth}
    if sender:
        data['sender'] = sender
    res = requests.post(url=url, json=data)
    if not res.ok:
        print(res.text)


def get_table_example():
    table = PrettyTable()
    table.field_names = ["City name", "Area", "Population", "Annual Rainfall"]
    table.add_row(["Adelaide", 1295, 1158259, 600.5])
    table.add_row(["Brisbane", 5905, 1857594, 1146.4])
    table.add_row(["Darwin", 112, 120900, 1714.7])
    table.add_row(["Hobart", 1357, 205556, 619.5])
    table.add_row(["Sydney", 2058, 4336374, 1214.8])
    return str(table)


if __name__ == '__main__':
    '''
    准备工作
    1、安装依赖库:pip install requests prettytable
    2、发送“获取发送token“给小鲁班，获取认证token，赋值给下面的auth变量
    '''
    personal_auth = ''  # 填写从小鲁班那里获取的token
    receiver_uid = ''  # 填写自己的uid，首字母+工号：“a00123456”

    # 1、普通消息，支持emoji表情，可在类似网站搜索然后复制：https://emojixd.com/
    send_msg('你好😍', receiver_uid, personal_auth)

    # 2、带有颜色和加粗
    send_msg('51.2.111.1: --- <span style="color:green;font-weight:bold;">PASS</span>', receiver_uid, personal_auth)

    # 3、表格
    table = get_table_example()
    send_msg(f'<span style="font-family:Consolas;font-weight:bold;">{str(table)}</span>', receiver_uid, personal_auth)

    # 4、图片，参考：http://3ms.huawei.com/hi/u/oGq1
    send_msg('你好\n#picture_aWyN#', receiver_uid, personal_auth)
