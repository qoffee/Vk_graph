import queue
import logging
import sys
import time
import requests
import pickle
import networkx as nx
from concurrent.futures import ThreadPoolExecutor
from settings import token, my_id, api_v, max_workers, delay, deep
import matplotlib.pyplot as plt

def GetLogger():
    log = logging.getLogger('vkgraph')
    log.setLevel(logging.DEBUG)
    ch = logging.StreamHandler()
    formatter = logging.Formatter('%(levelname)s - %(asctime)s - %(message)s')
    ch.setFormatter(formatter)
    log.addHandler(ch)
    return log
log = GetLogger()


def force(f, delay=delay):
    """При неудачном запросе сделать паузу и попробовать снова"""
    def tmp(*args, **kwargs):
        while True:
            try:
                res = f(*args, **kwargs)
                break
            except KeyError:
                time.sleep(delay)
        return res
    return tmp

class VkException(Exception):
    def __init__(self, message):
        self.message = message

    def __str__(self):
        return self.message


class VkFriends():
    """
    Находит друзей, находит общих друзей
    """
    parts = lambda lst, n=25: (lst[i:i + n] for i in iter(range(0, len(lst), n)))
    make_targets = lambda lst: ",".join(str(id) for id in lst)

    def __init__(self, *pargs):
        try:
            self.token, self.my_id, self.api_v, self.max_workers = pargs
            self.my_name, self.my_last_name, self.photo = self.base_info([self.my_id])
            self.all_friends, self.count_friends = self.friends(self.my_id)
        except VkException as error:
            sys.exit(error)

    def request_url(self, method_name, parameters, access_token=False):
        """read https://vk.com/dev/api_requests"""

        req_url = 'https://api.vk.com/method/{method_name}?{parameters}&v={api_v}'.format(
            method_name=method_name, api_v=self.api_v, parameters=parameters)

        if access_token:
            req_url = '{}&access_token={token}'.format(req_url, token=self.token)

        return req_url

    def base_info(self, ids):
        """read https://vk.com/dev/users.get"""
        r = requests.get(self.request_url('users.get', 'user_ids=%s&fields=photo' % (','.join(map(str, ids))))).json()
        if 'error' in r.keys():
            raise VkException('Error message: %s Error code: %s' % (r['error']['error_msg'], r['error']['error_code']))
        r = r['response'][0]
        # Проверяем, если id из settings.py не деактивирован
        if 'deactivated' in r.keys():
            raise VkException("User deactivated")
        return r['first_name'], r['last_name'], r['photo']

    def friends(self, id):
        """
        read https://vk.com/dev/friends.get
        Принимает идентификатор пользователя
        """
        # TODO: слишком много полей для всего сразу, город и страна не нужны для нахождения общих друзей
        r = requests.get(self.request_url('friends.get',
                'user_id=%s&fields=uid,first_name,last_name,photo,sex' % id)).json()
        if 'response' not in r:
            log.warn("couldn't get friends for {}".format("id"))
            return ({}, 0)
        r = r['response']
        #r = list(filter((lambda x: 'deactivated' not in x.keys()), r['items']))
        return {item['id']: item for item in r['items']}, r['count']

    def common_friends(self):
        """
        read https://vk.com/dev/friends.getMutual and read https://vk.com/dev/execute
        Возвращает в словаре кортежи с инфой о цели и списком общих друзей с инфой
        """
        result = []
        # разбиваем список на части - по 25 в каждой
        for i in VkFriends.parts(list(self.all_friends.keys())):
            r = requests.get(self.request_url('execute.getMutual',
                            'source=%s&targets=%s' % (self.my_id, VkFriends.make_targets(i)), access_token=True)).json()['response']
            for x, id in enumerate(i):
                result.append((self.all_friends[int(id)], [self.all_friends[int(i)] for i in r[x]] if r[x] else None))

        return result

    def deep_friends(self, deep):
        """
        Возвращает словарь с id пользователей, которые являются друзьями, или друзьями-друзей (и т.д. в зависимсти от
        deep - глубины поиска) указаннного пользователя
        """
        result = {}

        @force
        def worker(i):
            r = requests.get(self.request_url('execute.deepFriends', 'targets=%s' % VkFriends.make_targets(i), access_token=True)).json()['response']
            for x, id in enumerate(i):
                result[id] = tuple(r[x]["items"]) if r[x] else None

        def fill_result(friends):
            with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
                [pool.submit(worker, i) for i in VkFriends.parts(friends)]

        for i in range(deep):
            if result:
                # те айди, которых нет в ключах + не берем id:None
                fill_result(list(set([item for sublist in result.values() if sublist for item in sublist]) - set(result.keys())))
            else:
                url = self.request_url('friends.get', 'user_id=%s' % self.my_id, access_token=True)
                res = requests.get(url)
                print(res.json())
                fill_result(res.json()['response']["items"])

        return result

    def from_where_gender(self):
        """
        Возвращает кортеж из 3х частей
        0 -  сколько всего/в% друзей в определнной локации (country, city)
        1 - список, содержащий количество друзей того или иного пола. Где индекс
            0 - пол не указан
            1 - женский;
            2 - мужской;
        2 - сколько друзей родилось в тот или иной день
        """
        locations, all, genders, bdates = [{}, {}], [0, 0], [0, 0, 0], {}

        def calculate(dct, all):
            return {k: (dct[k], round(dct[k]/all * 100, 2)) for k, v in dct.items()}

        def constr(location, dct, ind):
            if location in dct.keys():
                place = dct[location]["title"]
                locations[ind][place] = 1 if place not in locations[ind] else locations[ind][place] + 1
                all[ind] += 1

        for i in self.all_friends.values():
            constr("country", i, 0)
            constr("city", i, 1)
            if "sex" in i.keys():
                genders[i["sex"]] += 1
            if "bdate" in i.keys():
                date = '.'.join(i["bdate"].split(".")[:2])
                bdates[date] = 1 if date not in bdates else bdates[date] + 1

        return (calculate(locations[0], all[0]), calculate(locations[1], all[1])), genders, bdates

    @staticmethod
    def save_load_deep_friends(myfile, sv, smth=None):
        if sv and smth:
            pickle.dump(smth, open(myfile, "wb"))
        else:
            return pickle.load(open(myfile, "rb"))




log = GetLogger()

def BuildGraph(vkfriends, deep=2, maxfriends=100):

    q = queue.Queue()
    q.put(a.my_id)

    G=nx.Graph()
    used = {}
    used[a.my_id] = True

    for i in range(deep): # friend depth
        log.info("starting iteration {}".format(i))
        nq = queue.Queue()

        while not q.empty(): # get id, build his friends
            log.info("graph contains {} nodes".format(len(G.nodes())))
            id = q.get()
            log.info("Fetching friends for {}".format(id))
            friends = a.friends(id)
            added = 0
            for k in friends[0]:
                if added >= maxfriends: break
                v = friends[0][k]
                fid = v['id']
                # log.info("Got friend {}".format(fid))
                G.add_edge(id,fid)
                added += 1
                if not fid in used:
                    nq.put(fid)

        q = nq

    log.info("done building")
    return G

def SaveGraph(G):
    log.info("started saving graph")
    nx.write_adjlist(G, "friends.adjlist")
    log.info("saved graph")
def LoadGraph():
    log.info("started loading graph")
    return nx.read_adjlist("friends.adjlist")
    log.info("loaded graph")
def DrawGraph(G):
    nx.draw(G,node_size=10)
    plt.show()


if __name__ == '__main__':
    a = VkFriends(token, my_id, api_v, max_workers)

    # G = BuildGraph(a)
    # SaveGraph(G)

    G = LoadGraph()
    DrawGraph(G)

    # DrawGraph(a)

    # friends = a.friends(232845615)
    # for f in friends[0]:
        # print(f, friends[0][f])
    # quit()

    # print(a.my_name, a.my_last_name, a.my_id, a.photo)
    # print(a.common_friends())
    # df = a.deep_friends(deep)
    # print(df)
    # VkFriends.save_load_deep_friends('deep_friends_dct', True, df)
    #print(pickle.load( open('deep_friends_dct', "rb" )))
    #print(a.from_where_gender())