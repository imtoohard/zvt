# -*- coding: utf-8 -*-
import enum
import json
import logging
import time
from typing import List, Union, Optional, Type

import pandas as pd
from sqlalchemy import Column, String, Text
from sqlalchemy.ext.declarative import declarative_base

from zvt.contract import IntervalLevel, EntityMixin
from zvt.contract import Mixin
from zvt.contract.api import get_data, df_to_db, get_db_session, del_data
from zvt.contract.reader import DataReader, DataListener
from zvt.contract.register import register_schema
from zvt.contract.zvt_context import factor_cls_registry
from zvt.utils.pd_utils import pd_is_not_null


class Indicator(object):
    def __init__(self) -> None:
        self.logger = logging.getLogger(self.__class__.__name__)
        self.indicators = []


class Transformer(Indicator):

    def __init__(self) -> None:
        super().__init__()

    def transform(self, input_df: pd.DataFrame) -> pd.DataFrame:
        """
        input_df format:

                                  col1    col2    col3    ...
        entity_id    timestamp
                                  1.2     0.5     0.3     ...
                                  1.0     0.7     0.2     ...

        the return result would change the columns and  keep the format

        :param input_df:
        :return:
        """
        g = input_df.groupby(level=0)
        if len(g.groups) == 1:
            entity_id = input_df.index[0][0]

            df = input_df.reset_index(level=0, drop=True)
            ret_df = self.transform_one(entity_id=entity_id, df=df)
            ret_df['entity_id'] = entity_id

            return ret_df.set_index('entity_id', append=True).swaplevel(0, 1)
        else:
            return g.apply(lambda x: self.transform_one(x.index[0][0], x.reset_index(level=0, drop=True)))

    def transform_one(self, entity_id, df: pd.DataFrame) -> pd.DataFrame:
        """
        df format:

                     col1    col2    col3    ...
        timestamp
                     1.2     0.5     0.3     ...
                     1.0     0.7     0.2     ...

        the return result would change the columns and  keep the format

        :param df:
        :return:
        """
        return df


class Accumulator(Indicator):

    def __init__(self, acc_window: int = 1) -> None:
        """

        :param acc_window: the window size of acc for computing,default is 1
        """
        super().__init__()
        self.acc_window = acc_window

    def acc(self, input_df: pd.DataFrame, acc_df: pd.DataFrame, states: dict) -> (pd.DataFrame, dict):
        """

        :param input_df: new input
        :param acc_df: previous result
        :return: new result and states
        """
        g = input_df.groupby(level=0)
        if len(g.groups) == 1:
            entity_id = input_df.index[0][0]

            df = input_df.reset_index(level=0, drop=True)
            if pd_is_not_null(acc_df):
                acc_one_df = acc_df.reset_index(level=0, drop=True)
            else:
                acc_one_df = None
            ret_df, state = self.acc_one(entity_id=entity_id, df=df, acc_df=acc_one_df, state=states.get(entity_id))
            ret_df['entity_id'] = entity_id

            return ret_df.set_index('entity_id', append=True).swaplevel(0, 1), {entity_id: state}
        else:
            new_states = {}

            def cal_acc(x):
                entity_id = x.index[0][0]
                if pd_is_not_null(acc_df):
                    acc_g = acc_df.groupby(level=0)
                    acc_one_df = acc_g.get_group(entity_id).reset_index(level=0, drop=True)
                else:
                    acc_one_df = None

                one_result, state = self.acc_one(entity_id=entity_id,
                                                 df=x.reset_index(level=0, drop=True),
                                                 acc_df=acc_one_df,
                                                 state=states.get(x.index[0][0]))

                new_states[entity_id] = state
                return one_result

            ret_df = g.apply(lambda x: cal_acc(x))
            return ret_df, new_states

    def acc_one(self, entity_id, df: pd.DataFrame, acc_df: pd.DataFrame, state: dict) -> (pd.DataFrame, dict):
        """
        df format:

                     col1    col2    col3    ...
        timestamp
                     1.2     0.5     0.3     ...
                     1.0     0.7     0.2     ...

        the new result and state

        :param df:
        :return:
        """
        return acc_df, state


class Scorer(object):
    def __init__(self) -> None:
        self.logger = logging.getLogger(self.__class__.__name__)

    def score(self, input_df: pd.DataFrame) -> object:
        return input_df


class FactorType(enum.Enum):
    filter = 'filter'
    score = 'score'
    state = 'state'


def register_class(target_class):
    if target_class.__name__ not in ('Factor', 'FilterFactor', 'ScoreFactor', 'StateFactor'):
        factor_cls_registry[target_class.__name__] = target_class


class FactorMeta(type):
    def __new__(meta, name, bases, class_dict):
        cls = type.__new__(meta, name, bases, class_dict)
        register_class(cls)
        return cls


FactorBase = declarative_base()


# 用于保存factor的状态
class FactorState(FactorBase, Mixin):
    __tablename__ = 'factor_state'
    # 因子名字
    factor_name = Column(String(length=128))

    # json string
    state = Column(Text())


register_schema(providers=['zvt'], db_name='factor_info', schema_base=FactorBase)


class Factor(DataReader, DataListener):
    factor_type: FactorType = None

    # define the schema for persist,its columns should be same as indicators in transformer or accumulator
    factor_schema: Type[Mixin] = None

    def __init__(self,
                 data_schema: Type[Mixin],
                 entity_schema: Type[EntityMixin] = None,
                 provider: str = None,
                 entity_provider: str = None,
                 entity_ids: List[str] = None,
                 exchanges: List[str] = None,
                 codes: List[str] = None,
                 the_timestamp: Union[str, pd.Timestamp] = None,
                 start_timestamp: Union[str, pd.Timestamp] = None,
                 end_timestamp: Union[str, pd.Timestamp] = None,
                 columns: List = None,
                 filters: List = None,
                 order: object = None,
                 limit: int = None,
                 level: Union[str, IntervalLevel] = None,
                 category_field: str = 'entity_id',
                 time_field: str = 'timestamp',
                 computing_window: int = None,
                 # child added arguments
                 keep_all_timestamp: bool = False,
                 fill_method: str = 'ffill',
                 effective_number: int = None,
                 transformer: Transformer = None,
                 accumulator: Accumulator = None,
                 need_persist: bool = False,
                 dry_run: bool = False,
                 factor_name: str = None,
                 clear_state: bool = False) -> None:
        """

        :param computing_window: the window size for computing factor
        :param keep_all_timestamp: whether fill all timestamp gap,default False
        :param fill_method:
        :param effective_number:
        :param transformer:
        :param accumulator:
        :param need_persist: whether persist factor
        :param dry_run: True for just computing factor, False for backtesting
        """

        super().__init__(data_schema, entity_schema, provider, entity_provider, entity_ids, exchanges, codes,
                         the_timestamp, start_timestamp, end_timestamp, columns, filters, order, limit, level,
                         category_field, time_field, computing_window)

        # define unique name of your factor if you want to keep factor state
        # the factor state is defined by factor_name and entity_id
        if not factor_name:
            self.factor_name = type(self).__name__.lower()
        else:
            self.factor_name = factor_name

        self.clear_state = clear_state

        self.keep_all_timestamp = keep_all_timestamp
        self.fill_method = fill_method
        self.effective_number = effective_number
        self.transformer = transformer
        self.accumulator = accumulator

        self.need_persist = need_persist
        self.dry_run = dry_run

        # 中间结果，不持久化
        # data_df->pipe_df
        self.pipe_df: pd.DataFrame = None

        # 计算因子的结果，可持久化,通过对pipe_df的计算得到
        # pipe_df->factor_df
        self.factor_df: pd.DataFrame = None

        # result_df是用于选股的标准df,通过对factor_df的计算得到
        # factor_df->result_df
        self.result_df: pd.DataFrame = None

        # entity_id:state
        self.states: dict = {}

        if self.clear_state:
            self.clear_state_data()
        elif self.need_persist:
            # read state
            states: List[FactorState] = FactorState.query_data(filters=[FactorState.factor_name == self.factor_name],
                                                               return_type='domain')
            if states:
                for state in states:
                    self.states[state.entity_id] = self.decode_state(json.loads(state.state))

            if self.dry_run:
                # 如果只是为了计算因子，只需要读取acc_window的factor_df
                if self.accumulator is not None:
                    self.factor_df = self.load_window_df(provider='zvt', data_schema=self.factor_schema,
                                                         window=accumulator.acc_window)
            else:
                self.factor_df = get_data(provider='zvt',
                                          data_schema=self.factor_schema,
                                          start_timestamp=self.start_timestamp,
                                          end_timestamp=self.end_timestamp,
                                          index=[self.category_field, self.time_field])

            # 根据已经计算的factor_df和computing_window来保留data_df
            # 因为读取data_df的目的是为了计算factor_df,选股和回测只依赖factor_df
            # 所以如果有持久化的factor_df,只需保留需要用于计算的data_df即可
            if pd_is_not_null(self.data_df) and self.computing_window:
                dfs = []
                for entity_id, df in self.data_df.groupby(level=0):
                    latest_laved = get_data(provider='zvt',
                                            data_schema=self.factor_schema,
                                            entity_id=entity_id,
                                            order=self.factor_schema.timestamp.desc(),
                                            limit=1,
                                            index=[self.category_field, self.time_field], return_type='domain')
                    if latest_laved:
                        df1 = df[df.timestamp < latest_laved[0].timestamp].iloc[-self.computing_window:]
                        if pd_is_not_null(df1):
                            df = df[df.timestamp >= df1.iloc[0].timestamp]
                    dfs.append(df)

                self.data_df = pd.concat(dfs)

        self.register_data_listener(self)

    def clear_state_data(self):
        del_data(FactorState, filters=[FactorState.factor_name == self.factor_name], provider='zvt')
        del_data(self.factor_schema, provider='zvt')

    def decode_state(self, state):
        return state

    def pre_compute(self):
        if not pd_is_not_null(self.pipe_df):
            self.pipe_df = self.data_df

    def do_compute(self):
        # 无状态的转换运算
        if pd_is_not_null(self.data_df) and self.transformer:
            self.pipe_df = self.transformer.transform(self.data_df)
        else:
            self.pipe_df = self.data_df

        # 有状态的累加运算
        if pd_is_not_null(self.pipe_df) and self.accumulator:
            self.factor_df, self.states = self.accumulator.acc(self.pipe_df, self.factor_df, self.states)
        else:
            self.factor_df = self.pipe_df

    def after_compute(self):
        if self.keep_all_timestamp:
            self.fill_gap()

        if self.need_persist:
            self.persist_factor()

    def compute(self):
        self.pre_compute()

        self.logger.info('>>>>>>')
        self.logger.info('do_compute start')
        start_time = time.time()
        self.do_compute()
        cost_time = time.time() - start_time
        self.logger.info('do_compute finished,cost_time:{}'.format(cost_time))

        self.logger.info('after_compute start')
        start_time = time.time()
        self.after_compute()
        cost_time = time.time() - start_time
        self.logger.info('after_compute finished,cost_time:{}'.format(cost_time))
        self.logger.info('<<<<<<')

    def drawer_main_df(self) -> Optional[pd.DataFrame]:
        return self.data_df

    def drawer_factor_df_list(self) -> Optional[List[pd.DataFrame]]:
        if (self.transformer is not None or self.accumulator is not None) and pd_is_not_null(self.factor_df):
            return [self.factor_df]
        return None

    def drawer_sub_df_list(self) -> Optional[List[pd.DataFrame]]:
        if (self.transformer is not None or self.accumulator is not None) and pd_is_not_null(self.result_df):
            return [self.result_df]
        return None

    def fill_gap(self):
        # 该操作较慢，只适合做基本面的运算
        idx = pd.date_range(self.start_timestamp, self.end_timestamp)
        new_index = pd.MultiIndex.from_product([self.result_df.index.levels[0], idx],
                                               names=['entity_id', self.time_field])
        self.result_df = self.result_df.loc[~self.result_df.index.duplicated(keep='first')]
        self.result_df = self.result_df.reindex(new_index)
        self.result_df = self.result_df.fillna(method=self.fill_method, limit=self.effective_number)

    def on_data_loaded(self, data: pd.DataFrame):
        self.compute()

    def on_data_changed(self, data: pd.DataFrame):
        """
        overwrite it for computing after data added

        Parameters
        ----------
        data :
        """
        self.compute()

    def on_entity_data_changed(self, entity, added_data: pd.DataFrame):
        """
        overwrite it for computing after entity data added

        Parameters
        ----------
        entity :
        added_data :
        """
        pass

    def persist_factor(self):
        df_to_db(df=self.factor_df, data_schema=self.factor_schema, provider='zvt', force_update=False)
        if self.states:
            session = get_db_session(provider='zvt', data_schema=FactorState)
            for entity_id, state in self.states:
                domain_id = f'{self.factor_name}_{entity_id}'
                factor_state: FactorState = session.query(FactorState).get(domain_id)
                state_str = json.dumps(state)
                if factor_state:
                    factor_state.state = state_str
                else:
                    factor_state = FactorState(id=domain_id, entity_id=entity_id, factor_name=self.factor_name,
                                               state=json.dumps(state))
                    session.add(factor_state)
            session.commit()


class FilterFactor(Factor):
    factor_type = FactorType.filter


class ScoreFactor(Factor):
    factor_type = FactorType.score

    def __init__(self, data_schema: Mixin, entity_schema: EntityMixin = None, provider: str = None,
                 entity_provider: str = None, entity_ids: List[str] = None, exchanges: List[str] = None,
                 codes: List[str] = None, the_timestamp: Union[str, pd.Timestamp] = None,
                 start_timestamp: Union[str, pd.Timestamp] = None, end_timestamp: Union[str, pd.Timestamp] = None,
                 columns: List = None, filters: List = None, order: object = None, limit: int = None,
                 level: Union[str, IntervalLevel] = IntervalLevel.LEVEL_1DAY, category_field: str = 'entity_id',
                 time_field: str = 'timestamp', computing_window: int = None, keep_all_timestamp: bool = False,
                 fill_method: str = 'ffill', effective_number: int = None, transformer: Transformer = None,
                 accumulator: Accumulator = None, need_persist: bool = False, dry_run: bool = False,
                 factor_name: str = None, clear_state: bool = False,
                 scorer: Scorer = None) -> None:
        self.scorer = scorer
        super().__init__(data_schema, entity_schema, provider, entity_provider, entity_ids, exchanges, codes,
                         the_timestamp, start_timestamp, end_timestamp, columns, filters, order, limit, level,
                         category_field, time_field, computing_window, keep_all_timestamp, fill_method,
                         effective_number, transformer, accumulator, need_persist, dry_run, factor_name, clear_state)

    def do_compute(self):
        super().do_compute()

        if pd_is_not_null(self.factor_df) and self.scorer:
            self.result_df = self.scorer.score(self.factor_df)


class StateFactor(Factor):
    factor_type = FactorType.state
    states = []

    def get_state(self, timestamp, entity_id):
        pass

    def get_short_state(self):
        pass

    def get_long_state(self):
        pass


# the __all__ is generated
__all__ = ['Indicator', 'Transformer', 'Accumulator', 'Scorer', 'FactorType', 'Factor', 'FilterFactor', 'ScoreFactor',
           'StateFactor']
