# pragma pylint: disable=W0603
""" Edge positioning package """
import logging
from typing import Any, Dict
from collections import namedtuple
import arrow

import numpy as np
import utils_find_1st as utf1st
from pandas import DataFrame

import freqtrade.optimize as optimize
from freqtrade.arguments import Arguments
from freqtrade.arguments import TimeRange
from freqtrade.strategy.interface import SellType


logger = logging.getLogger(__name__)


class Edge():
    """
    Calculates Win Rate, Risk Reward Ratio, Expectancy
    against historical data for a give set of markets and a strategy
    it then adjusts stoploss and position size accordingly
    and force it into the strategy
    Author: https://github.com/mishaker
    """

    config: Dict = {}
    _cached_pairs: Dict[str, Any] = {}  # Keeps a list of pairs

    # pair info data type
    _pair_info = namedtuple(
        'pair_info',
        ['stoploss', 'winrate', 'risk_reward_ratio', 'required_risk_reward', 'expectancy',
         'nb_trades', 'avg_trade_duration']
    )

    def __init__(self, config: Dict[str, Any], exchange, strategy) -> None:

        self.config = config
        self.exchange = exchange
        self.strategy = strategy
        self.ticker_interval = self.strategy.ticker_interval
        self.tickerdata_to_dataframe = self.strategy.tickerdata_to_dataframe
        self.get_timeframe = optimize.get_timeframe
        self.advise_sell = self.strategy.advise_sell
        self.advise_buy = self.strategy.advise_buy

        self.edge_config = self.config.get('edge', {})
        self._cached_pairs: Dict[str, Any] = {}  # Keeps a list of pairs

        self._total_capital: float = self.config['stake_amount']
        self._allowed_risk: float = self.edge_config.get('allowed_risk')
        self._since_number_of_days: int = self.edge_config.get('calculate_since_number_of_days', 14)
        self._last_updated: int = 0  # Timestamp of pairs last updated time
        self._refresh_pairs = True

        self._stoploss_range_min = float(self.edge_config.get('stoploss_range_min', -0.01))
        self._stoploss_range_max = float(self.edge_config.get('stoploss_range_max', -0.05))
        self._stoploss_range_step = float(self.edge_config.get('stoploss_range_step', -0.001))

        # calculating stoploss range
        self._stoploss_range = np.arange(
            self._stoploss_range_min,
            self._stoploss_range_max,
            self._stoploss_range_step
        )

        self._timerange: TimeRange = Arguments.parse_timerange("%s-" % arrow.now().shift(
            days=-1 * self._since_number_of_days).format('YYYYMMDD'))

        self.fee = self.exchange.get_fee()

    def calculate(self) -> bool:
        pairs = self.config['exchange']['pair_whitelist']
        heartbeat = self.edge_config.get('process_throttle_secs')

        if (self._last_updated > 0) and (
                self._last_updated + heartbeat > arrow.utcnow().timestamp):
            return False

        data: Dict[str, Any] = {}
        logger.info('Using stake_currency: %s ...', self.config['stake_currency'])
        logger.info('Using local backtesting data (using whitelist in given config) ...')

        data = optimize.load_data(
            self.config['datadir'],
            pairs=pairs,
            ticker_interval=self.ticker_interval,
            refresh_pairs=self._refresh_pairs,
            exchange=self.exchange,
            timerange=self._timerange
        )

        if not data:
            # Reinitializing cached pairs
            self._cached_pairs = {}
            logger.critical("No data found. Edge is stopped ...")
            return False

        preprocessed = self.tickerdata_to_dataframe(data)

        # Print timeframe
        min_date, max_date = self.get_timeframe(preprocessed)
        logger.info(
            'Measuring data from %s up to %s (%s days) ...',
            min_date.isoformat(),
            max_date.isoformat(),
            (max_date - min_date).days
        )
        headers = ['date', 'buy', 'open', 'close', 'sell', 'high', 'low']

        trades: list = []
        for pair, pair_data in preprocessed.items():
            # Sorting dataframe by date and reset index
            pair_data = pair_data.sort_values(by=['date'])
            pair_data = pair_data.reset_index(drop=True)

            ticker_data = self.advise_sell(
                self.advise_buy(pair_data, {'pair': pair}), {'pair': pair})[headers].copy()

            trades += self._find_trades_for_stoploss_range(ticker_data, pair, self._stoploss_range)

        # If no trade found then exit
        if len(trades) == 0:
            return False

        # Fill missing, calculable columns, profit, duration , abs etc.
        trades_df = self._fill_calculable_fields(DataFrame(trades))
        self._cached_pairs = self._process_expectancy(trades_df)
        self._last_updated = arrow.utcnow().timestamp

        # Not a nice hack but probably simplest solution:
        # When backtest load data it loads the delta between disk and exchange
        # The problem is that exchange consider that recent.
        # it is but it is incomplete (c.f. _async_get_candle_history)
        # So it causes get_signal to exit cause incomplete ticker_hist
        # A patch to that would be update _pairs_last_refresh_time of exchange
        # so it will download again all pairs
        # Another solution is to add new data to klines instead of reassigning it:
        # self.klines[pair].update(data) instead of self.klines[pair] = data in exchange package.
        # But that means indexing timestamp and having a verification so that
        # there is no empty range between two timestaps (recently added and last
        # one)
        self.exchange._pairs_last_refresh_time = {}

        return True

    def stake_amount(self, pair: str) -> float:
        stoploss = self._cached_pairs[pair].stoploss
        allowed_capital_at_risk = round(self._total_capital * self._allowed_risk, 5)
        position_size = abs(round((allowed_capital_at_risk / stoploss), 5))
        return position_size

    def stoploss(self, pair: str) -> float:
        return self._cached_pairs[pair].stoploss

    def adjust(self, pairs) -> list:
        """
        Filters out and sorts "pairs" according to Edge calculated pairs
        """

        final = []
        for pair, info in self._cached_pairs.items():
            if info.expectancy > float(self.edge_config.get('minimum_expectancy', 0.2)) and \
                info.winrate > float(self.edge_config.get('minimum_winrate', 0.60)) and \
                    pair in pairs:
                final.append(pair)

        if final:
            logger.info('Edge validated only %s', final)
        else:
            logger.info('Edge removed all pairs as no pair with minimum expectancy was found !')

        return final

    def _fill_calculable_fields(self, result: DataFrame) -> DataFrame:
        """
        The result frame contains a number of columns that are calculable
        from other columns. These are left blank till all rows are added,
        to be populated in single vector calls.

        Columns to be populated are:
        - Profit
        - trade duration
        - profit abs
        :param result Dataframe
        :return: result Dataframe
        """

        # stake and fees
        # stake = 0.015
        # 0.05% is 0.0005
        # fee = 0.001

        stake = self.config.get('stake_amount')
        fee = self.fee

        open_fee = fee / 2
        close_fee = fee / 2

        result['trade_duration'] = result['close_time'] - result['open_time']

        result['trade_duration'] = result['trade_duration'].map(
            lambda x: int(x.total_seconds() / 60))

        # Spends, Takes, Profit, Absolute Profit

        # Buy Price
        result['buy_vol'] = stake / result['open_rate']  # How many target are we buying
        result['buy_fee'] = stake * open_fee
        result['buy_spend'] = stake + result['buy_fee']  # How much we're spending

        # Sell price
        result['sell_sum'] = result['buy_vol'] * result['close_rate']
        result['sell_fee'] = result['sell_sum'] * close_fee
        result['sell_take'] = result['sell_sum'] - result['sell_fee']

        # profit_percent
        result['profit_percent'] = (result['sell_take'] - result['buy_spend']) / result['buy_spend']

        # Absolute profit
        result['profit_abs'] = result['sell_take'] - result['buy_spend']

        return result

    def _process_expectancy(self, results: DataFrame) -> Dict[str, Any]:
        """
        This calculates WinRate, Required Risk Reward, Risk Reward and Expectancy of all pairs
        The calulation will be done per pair and per strategy.
        """
        # Removing pairs having less than min_trades_number
        min_trades_number = self.edge_config.get('min_trade_number', 10)
        results = results.groupby(['pair', 'stoploss']).filter(lambda x: len(x) > min_trades_number)
        ###################################

        # Removing outliers (Only Pumps) from the dataset
        # The method to detect outliers is to calculate standard deviation
        # Then every value more than (standard deviation + 2*average) is out (pump)
        #
        # Removing Pumps
        if self.edge_config.get('remove_pumps', False):
            results = results.groupby(['pair', 'stoploss']).apply(
                lambda x: x[x['profit_abs'] < 2 * x['profit_abs'].std() + x['profit_abs'].mean()])
        ##########################################################################

        # Removing trades having a duration more than X minutes (set in config)
        max_trade_duration = self.edge_config.get('max_trade_duration_minute', 1440)
        results = results[results.trade_duration < max_trade_duration]
        #######################################################################

        if results.empty:
            return {}

        groupby_aggregator = {
            'profit_abs': [
                ('nb_trades', 'count'),  # number of all trades
                ('profit_sum', lambda x: x[x > 0].sum()),  # cumulative profit of all winning trades
                ('loss_sum', lambda x: abs(x[x < 0].sum())),  # cumulative loss of all losing trades
                ('nb_win_trades', lambda x: x[x > 0].count())  # number of winning trades
            ],
            'trade_duration': [('avg_trade_duration', 'mean')]
        }

        # Group by (pair and stoploss) by applying above aggregator
        df = results.groupby(['pair', 'stoploss'])['profit_abs', 'trade_duration'].agg(
            groupby_aggregator).reset_index(col_level=1)

        # Dropping level 0 as we don't need it
        df.columns = df.columns.droplevel(0)

        # Calculating number of losing trades, average win and average loss
        df['nb_loss_trades'] = df['nb_trades'] - df['nb_win_trades']
        df['average_win'] = df['profit_sum'] / df['nb_win_trades']
        df['average_loss'] = df['loss_sum'] / df['nb_loss_trades']

        # Win rate = number of profitable trades / number of trades
        df['winrate'] = df['nb_win_trades'] / df['nb_trades']

        # risk_reward_ratio = average win / average loss
        df['risk_reward_ratio'] = df['average_win'] / df['average_loss']

        # required_risk_reward = (1 / winrate) - 1
        df['required_risk_reward'] = (1 / df['winrate']) - 1

        # expectancy = (risk_reward_ratio * winrate) - (lossrate)
        df['expectancy'] = (df['risk_reward_ratio'] * df['winrate']) - (1 - df['winrate'])

        # sort by expectancy and stoploss
        df = df.sort_values(by=['expectancy', 'stoploss'], ascending=False).groupby(
            'pair').first().sort_values(by=['expectancy'], ascending=False).reset_index()

        final = {}
        for x in df.itertuples():
            info = {
                'stoploss': x.stoploss,
                'winrate': x.winrate,
                'risk_reward_ratio': x.risk_reward_ratio,
                'required_risk_reward': x.required_risk_reward,
                'expectancy': x.expectancy,
                'nb_trades': x.nb_trades,
                'avg_trade_duration': x.avg_trade_duration
            }
            final[x.pair] = self._pair_info(**info)

        # Returning a list of pairs in order of "expectancy"
        return final

    def _find_trades_for_stoploss_range(self, ticker_data, pair, stoploss_range):
        buy_column = ticker_data['buy'].values
        sell_column = ticker_data['sell'].values
        date_column = ticker_data['date'].values
        ohlc_columns = ticker_data[['open', 'high', 'low', 'close']].values

        result: list = []
        for stoploss in stoploss_range:
            result += self._detect_next_stop_or_sell_point(
                buy_column, sell_column, date_column, ohlc_columns, round(stoploss, 6), pair
            )

        return result

    def _detect_next_stop_or_sell_point(self, buy_column, sell_column, date_column,
                                        ohlc_columns, stoploss, pair, start_point=0):
        """
        Iterate through ohlc_columns recursively in order to find the next trade
        Next trade opens from the first buy signal noticed to
        The sell or stoploss signal after it.
        It then calls itself cutting OHLC, buy_column, sell_colum and date_column
        Cut from (the exit trade index) + 1
        Author: https://github.com/mishaker
        """

        result: list = []
        open_trade_index = utf1st.find_1st(buy_column, 1, utf1st.cmp_equal)

        # return empty if we don't find trade entry (i.e. buy==1) or
        # we find a buy but at the of array
        if open_trade_index == -1 or open_trade_index == len(buy_column) - 1:
            return []
        else:
            open_trade_index += 1  # when a buy signal is seen,
            # trade opens in reality on the next candle

        stop_price_percentage = stoploss + 1
        open_price = ohlc_columns[open_trade_index, 0]
        stop_price = (open_price * stop_price_percentage)

        # Searching for the index where stoploss is hit
        stop_index = utf1st.find_1st(
            ohlc_columns[open_trade_index:, 2], stop_price, utf1st.cmp_smaller)

        # If we don't find it then we assume stop_index will be far in future (infinite number)
        if stop_index == -1:
            stop_index = float('inf')

        # Searching for the index where sell is hit
        sell_index = utf1st.find_1st(sell_column[open_trade_index:], 1, utf1st.cmp_equal)

        # If we don't find it then we assume sell_index will be far in future (infinite number)
        if sell_index == -1:
            sell_index = float('inf')

        # Check if we don't find any stop or sell point (in that case trade remains open)
        # It is not interesting for Edge to consider it so we simply ignore the trade
        # And stop iterating there is no more entry
        if stop_index == sell_index == float('inf'):
            return []

        if stop_index <= sell_index:
            exit_index = open_trade_index + stop_index
            exit_type = SellType.STOP_LOSS
            exit_price = stop_price
        elif stop_index > sell_index:
            # if exit is SELL then we exit at the next candle
            exit_index = open_trade_index + sell_index + 1

            # check if we have the next candle
            if len(ohlc_columns) - 1 < exit_index:
                return []

            exit_type = SellType.SELL_SIGNAL
            exit_price = ohlc_columns[exit_index, 0]

        trade = {'pair': pair,
                 'stoploss': stoploss,
                 'profit_percent': '',
                 'profit_abs': '',
                 'open_time': date_column[open_trade_index],
                 'close_time': date_column[exit_index],
                 'open_index': start_point + open_trade_index,
                 'close_index': start_point + exit_index,
                 'trade_duration': '',
                 'open_rate': round(open_price, 15),
                 'close_rate': round(exit_price, 15),
                 'exit_type': exit_type
                 }

        result.append(trade)

        # Calling again the same function recursively but giving
        # it a view of exit_index till the end of array
        return result + self._detect_next_stop_or_sell_point(
            buy_column[exit_index:],
            sell_column[exit_index:],
            date_column[exit_index:],
            ohlc_columns[exit_index:],
            stoploss,
            pair,
            (start_point + exit_index)
        )