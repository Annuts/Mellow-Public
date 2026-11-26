"""Small, explicit communication and evidence budgets."""
import re
import unicodedata
from datetime import datetime
from zoneinfo import ZoneInfo
from .features import DEFAULT_TIMEZONE

CONSTITUTION = ('沟通约定：直接回应，不以复述、附和开场，不以“还需要帮忙吗”收尾。'
    '简单问题短答，复杂问题再展开，不机械凑齐段落。用户疲惫时只给一个最小下一步。'
    '区分已确认、合理推断和猜测，不把猜测说成事实。温暖但克制，不煽情、不讨好；'
    '小满也可称满娘，但不强行扮演亲密关系。'
    '先接住这轮真正要解决的问题。用户纠正时沿用上一轮仍然有效的条件，只改被纠正的地方。不把常住地、餐厅、旅行计划或上次查过的城市当成当前位置。问当地天气时只用本轮点名或已获准的近期城市，不确定就只问城市。天气、票价等时效事实须有对应地点、日期的查询结果，给出来源。'
    '留意本地时间和聊天间隔：自然地问候、久别重逢，深夜温柔提醒休息；不机械报时，不编造日期。')


def extraction_prompt(source, preferences):
    from .profile import SPEC
    import json
    return (SPEC['policy'] + '\n可用槽位：' + json.dumps(SPEC['slots'], ensure_ascii=False) + '\n消息当地时间：' +
        datetime.fromisoformat(source.get('occurred_at') or source['updated_at']).astimezone(
            ZoneInfo(preferences.get('timezone', DEFAULT_TIMEZONE))).isoformat())


def memory_key(value):
    return re.sub(r'\s+', '_', unicodedata.normalize('NFKC', value).strip().lower())


def budget_day(state):
    from .usage import budget_day as local_day
    return local_day(state)
