#!/usr/bin/perl
# Paper-only 6-month dip_hold backtest.
# Ports swing_backtest.py simulate_book dip_hold exactly.
# Does not retune -2% / 3-close. No live orders. No invented bars.
use strict;
use warnings;
use JSON::PP;
use POSIX qw(strftime);
use Time::Local qw(timegm);

# --- fixed rule (must match 30d test) ---
use constant ONE_WAY_FEE      => 0.0012;
use constant STARTING_BALANCE => 10_000.0;
use constant DIP_RET          => -0.02;
use constant DIP_HOLD_CLOSES  => 3;

my @SYMBOLS = qw(BTCUSDT ETHUSDT SOLUSDT DOGEUSDT);
my $VISION  = "https://data-api.binance.vision/api/v3/klines";
my $BINANCE = "https://api.binance.com/api/v3/klines";
my $UA      = "stablebot/0.1 (research paper-trading; no live orders)";
my $OUT_DIR = "/workspace/crypto-bot/data/backtests";

# 6m window
my $WARMUP_START = timegm(0, 0, 0, 10, 1, 2026);   # 2026-02-10 00:00Z
my $WINDOW_START = timegm(0, 0, 0, 15, 1, 2026);   # 2026-02-15 00:00Z
my $WINDOW_END   = timegm(0, 0, 9, 15, 7, 2026);   # 2026-08-15 09:00Z

# 30d reference (same rule, not a target)
my $REF_30D_F25 = 400.85;
my $REF_30D_BTC = 213.62;

sub iso {
    my ($t) = @_;
    return strftime("%Y-%m-%dT%H:%M:%SZ", gmtime($t));
}
sub ymd {
    my ($t) = @_;
    return strftime("%Y-%m-%d", gmtime($t));
}
sub parse_iso {
    my ($s) = @_;
    $s =~ s/Z$//;
    my ($Y,$m,$d,$H,$M,$S) = $s =~ /^(\d+)-(\d+)-(\d+)T(\d+):(\d+):(\d+)/;
    return timegm($S, $M, $H, $d, $m - 1, $Y);
}

sub http_json {
    my ($url) = @_;
    my $cmd = sprintf(
        "curl -sS --max-time 25 -A %s -H %s %s",
        quotemeta($UA),
        quotemeta("Accept: application/json"),
        quotemeta($url),
    );
    my $raw = `$cmd`;
    my $rc = $? >> 8;
    die "curl $rc for $url" if $rc != 0 || !defined $raw || $raw eq "";
    my $j = JSON::PP->new->decode($raw);
    return $j;
}

sub fetch_klines {
    my ($symbol, $start, $end) = @_;
    my $start_ms = $start * 1000;
    my $end_ms   = $end * 1000;
    my $step_ms  = 3_600_000;
    my @errors;
    for my $base ($VISION, $BINANCE) {
        my ($got, $got_src);
        my $ok = eval {
            my @out;
            my $cursor = $start_ms;
            my $pages = 0;
            while ($cursor <= $end_ms && $pages < 40) {
                my $url = sprintf(
                    "%s?symbol=%s&interval=1h&startTime=%s&endTime=%s&limit=1000",
                    $base, $symbol, $cursor, $end_ms
                );
                my $rows = http_json($url);
                die "$base $symbol: not an array" unless ref($rows) eq "ARRAY";
                last unless @$rows;
                for my $row (@$rows) {
                    my $ot = int($row->[0]) / 1000;
                    my $ct = int($row->[6]) / 1000;
                    my $o = 0 + $row->[1];
                    my $h = 0 + $row->[2];
                    my $l = 0 + $row->[3];
                    my $c = 0 + $row->[4];
                    my $v = 0 + $row->[5];
                    next if $o <= 0 || $h <= 0 || $l <= 0 || $c <= 0;
                    push @out, { ot => $ot, ct => $ct, o => $o, h => $h, l => $l, c => $c, v => $v };
                }
                my $last_ot_ms = int($rows->[-1][0]);
                my $nxt = $last_ot_ms + $step_ms;
                last if $nxt <= $cursor;
                $cursor = $nxt;
                $pages++;
                last if @$rows < 1000;
            }
            die "empty" unless @out;
            # dedupe by ot
            my %seen;
            my @dedup;
            for my $b (sort { $a->{ot} <=> $b->{ot} } @out) {
                next if $seen{$b->{ot}}++;
                next unless $start <= $b->{ot} && $b->{ot} <= $end;
                push @dedup, $b;
            }
            die "empty after filter" unless @dedup;
            ($got, $got_src) = (\@dedup, $base);
            1;
        };
        if ($ok && $got) {
            return ($got, $got_src);
        }
        push @errors, "$base: " . ($@ || "no data");
    }
    die "$symbol 1h failed: " . join("; ", @errors);
}

sub resample_daily {
    my ($bars) = @_;
    my %g;
    for my $b (@$bars) {
        push @{ $g{ ymd($b->{ot}) } }, $b;
    }
    my @out;
    for my $date (sort keys %g) {
        my @chunk = sort { $a->{ot} <=> $b->{ot} } @{ $g{$date} };
        my $n = scalar @chunk;
        my $hi = $chunk[0]{h};
        my $lo = $chunk[0]{l};
        my $vol = 0;
        for my $x (@chunk) {
            $hi = $x->{h} if $x->{h} > $hi;
            $lo = $x->{l} if $x->{l} < $lo;
            $vol += $x->{v};
        }
        push @out, {
            date     => $date,
            ot       => $chunk[0]{ot},
            ct       => $chunk[-1]{ct},
            o        => $chunk[0]{o},
            h        => $hi,
            l        => $lo,
            c        => $chunk[-1]{c},
            v        => $vol,
            n_hours  => $n,
            complete => $n >= 24 ? 1 : 0,
        };
    }
    return \@out;
}

sub last_px {
    my ($h1, $idx, $sym, $t, $prefer) = @_;
    if (exists $idx->{$sym}{$t}) {
        my $bar = $h1->{$sym}[ $idx->{$sym}{$t} ];
        return $prefer eq "open" ? $bar->{o} : $bar->{c};
    }
    my $bars = $h1->{$sym};
    for (my $i = $#$bars; $i >= 0; $i--) {
        if ($bars->[$i]{ot} <= $t) {
            return $bars->[$i]{c};
        }
    }
    return undef;
}

sub equity_at {
    my ($cash, $positions, $h1, $idx, $t, $prefer) = @_;
    my $eq = $cash;
    for my $sym (keys %$positions) {
        my $pos = $positions->{$sym};
        my $px = last_px($h1, $idx, $sym, $t, $prefer);
        $px = $pos->{entry_px} unless defined $px;
        $eq += $pos->{qty} * $px;
    }
    return $eq;
}

sub simulate_book {
    my ($strategy, $book, $h1, $daily, $data_source, $skipped) = @_;
    my @symbols;
    my $allocation;
    if ($book eq "four25") {
        @symbols = grep { exists $h1->{$_} } @SYMBOLS;
        $allocation = 1.0 / (scalar(@symbols) || 1);
    } else {
        @symbols = grep { exists $h1->{$_} } ("BTCUSDT");
        $allocation = 1.0;
    }
    my @notes = (
        "data source: $data_source",
        "fee assumption: 10 bps taker + 1 bp half-spread each side (12 bps one-way, 24 bps round trip); OHLC fill, spread counted in fee only",
        "no lookahead: daily signal on UTC daily close, fill next day's open; dip_hold recovery and 3-close time-stop exit on that daily close",
        "paper only — no live orders",
        "fixed rules, not tuned after seeing the window",
        "daily return for dip_hold is close-to-close (close_t / close_{t-1} - 1)",
        "no leverage; long-only; no stacking on dip_hold",
        "rule copied from 30d test: prior daily ret <= -2%, exit after 3 daily closes or close above signal-day open",
    );
    die "no symbols for $book $strategy" unless @symbols;

    my %idx;
    my %daily_by_date;
    my %daily_list;
    my %daily_i;
    my %next_day_open;
    for my $s (@symbols) {
        $idx{$s} = {};
        for my $i (0 .. $#{ $h1->{$s} }) {
            $idx{$s}{ $h1->{$s}[$i]{ot} } = $i;
        }
        my $series = $daily->{$s};
        $daily_list{$s} = $series;
        $daily_by_date{$s} = { map { $_->{date} => $_ } @$series };
        $daily_i{$s} = {};
        for my $i (0 .. $#$series) {
            $daily_i{$s}{ $series->[$i]{date} } = $i;
        }
        my %nd;
        for my $i (0 .. $#$series - 1) {
            $nd{ $series->[$i]{date} } = $series->[$i + 1]{ot};
        }
        $next_day_open{$s} = \%nd;
    }

    my %timeset;
    for my $s (@symbols) {
        for my $b (@{ $h1->{$s} }) {
            $timeset{ $b->{ot} } = 1
                if $WINDOW_START <= $b->{ot} && $b->{ot} <= $WINDOW_END;
        }
    }
    my @times = sort { $a <=> $b } keys %timeset;
    die "no bars inside window for $book $strategy" unless @times;
    my $last_t  = $times[-1];
    my $first_t = $times[0];

    my $cash = STARTING_BALANCE;
    my %positions;
    my %pending;
    my @trades;
    my $fees_paid = 0.0;
    my @equity_curve;
    my %daily_equity;
    my %daily_trade_count;
    my %daily_fees;

    my $close_pos = sub {
        my ($sym, $t, $px, $reason) = @_;
        my $pos = delete $positions{$sym};
        return unless $pos;
        my $exit_notional = $pos->{qty} * $px;
        my $exit_fee = ONE_WAY_FEE * $exit_notional;
        $cash += $exit_notional - $exit_fee;
        $fees_paid += $exit_fee;
        my $pnl = $pos->{qty} * ($px - $pos->{entry_px}) - $pos->{entry_fee} - $exit_fee;
        push @trades, {
            symbol     => $sym,
            entry_time => iso($pos->{entry_time}),
            entry_px   => $pos->{entry_px},
            exit_time  => iso($t),
            exit_px    => $px,
            qty        => $pos->{qty},
            notional   => $pos->{notional},
            pnl        => $pnl,
            fees       => $pos->{entry_fee} + $exit_fee,
            reason     => $reason,
        };
        my $d = ymd($t);
        $daily_trade_count{$d} = ($daily_trade_count{$d} || 0) + 1;
        $daily_fees{$d} = ($daily_fees{$d} || 0) + $exit_fee;
    };

    my $open_many = sub {
        my (@entries) = @_;
        @entries = grep { !exists $positions{ $_->[0] } } @entries;
        return unless @entries;
        my $t0 = $entries[0][1];
        my $eq = equity_at($cash, \%positions, $h1, \%idx, $t0, "open");
        return if $eq <= 0;
        my %raw;
        $raw{ $_->[0] } = $allocation * $eq for @entries;
        my $total_cost = 0;
        $total_cost += $_ * (1.0 + ONE_WAY_FEE) for values %raw;
        return if $total_cost <= 0;
        if ($total_cost > $cash + 1e-9) {
            my $scale = $cash / $total_cost;
            $raw{$_} *= $scale for keys %raw;
        }
        for my $e (@entries) {
            my ($sym, $t, $px, $reason, $sig_open) = @$e;
            my $notional = $raw{$sym};
            next if $notional <= 0 || $px <= 0;
            my $qty = $notional / $px;
            my $entry_fee = ONE_WAY_FEE * $notional;
            my $cost = $notional + $entry_fee;
            if ($cost > $cash + 1e-9) {
                $notional = $cash / (1.0 + ONE_WAY_FEE);
                next if $notional <= 0;
                $qty = $notional / $px;
                $entry_fee = ONE_WAY_FEE * $notional;
                $cost = $notional + $entry_fee;
            }
            $cash -= $cost;
            $positions{$sym} = {
                symbol      => $sym,
                entry_time  => $t,
                entry_px    => $px,
                qty         => $qty,
                notional    => $notional,
                entry_fee   => $entry_fee,
                signal_open => $sig_open,
                closes_held => 0,
            };
            $fees_paid += $entry_fee;
            my $d = ymd($t);
            $daily_fees{$d} = ($daily_fees{$d} || 0) + $entry_fee;
        }
    };

    my $is_day_close_bar = sub {
        my ($sym, $t) = @_;
        return 0 unless exists $idx{$sym}{$t};
        my $i = $idx{$sym}{$t};
        my $bars = $h1->{$sym};
        my $date = ymd($bars->[$i]{ot});
        return 1 if $i + 1 >= @$bars;
        return ymd($bars->[$i + 1]{ot}) ne $date;
    };

    for my $t (@times) {
        # 1) pending fills at this bar's open
        my @open_batch;
        for my $s (@symbols) {
            my $pend = $pending{$s};
            if (!$pend || $pend->{fill_time} != $t || !$pend->{use_open}) {
                if ($pend && $pend->{fill_time} < $t) {
                    delete $pending{$s};
                }
                next;
            }
            unless (exists $idx{$s}{$t}) {
                delete $pending{$s};
                next;
            }
            my $px = $h1->{$s}[ $idx{$s}{$t} ]{o};
            if ($pend->{kind} eq "exit" && exists $positions{$s}) {
                $close_pos->($s, $t, $px, $pend->{reason});
                delete $pending{$s};
            } elsif ($pend->{kind} eq "entry" && !exists $positions{$s}) {
                push @open_batch, [ $s, $t, $px, $pend->{reason}, $pend->{signal_open} ];
                delete $pending{$s};
            } else {
                delete $pending{$s};
            }
        }
        $open_many->(@open_batch) if @open_batch;

        # 2) daily-close exits and new signals
        for my $s (@symbols) {
            next unless $is_day_close_bar->($s, $t);
            next unless exists $idx{$s}{$t};
            my $date = ymd($t);
            my $dbar = $daily_by_date{$s}{$date};
            next unless $dbar;

            if ($strategy eq "dip_hold" && exists $positions{$s}) {
                my $pos = $positions{$s};
                $pos->{closes_held}++;
                my $recovered = defined $pos->{signal_open} && $dbar->{c} > $pos->{signal_open};
                my $timed = $pos->{closes_held} >= DIP_HOLD_CLOSES;
                if ($recovered) {
                    $close_pos->($s, $dbar->{ct}, $dbar->{c}, "dip_recover_close");
                } elsif ($timed) {
                    $close_pos->($s, $dbar->{ct}, $dbar->{c}, "dip_3close_time");
                }
            }

            my $nxt = $next_day_open{$s}{$date};
            next unless defined $nxt;
            next if $nxt < $WINDOW_START || $nxt > $WINDOW_END;

            if ($strategy eq "dip_hold") {
                my $di = $daily_i{$s}{$date};
                next unless defined $di && $di >= 1;
                my $prev = $daily_list{$s}[$di - 1];
                next if $prev->{c} <= 0;
                my $ret = $dbar->{c} / $prev->{c} - 1.0;
                if ($ret <= DIP_RET && !exists $positions{$s} && !exists $pending{$s}) {
                    $pending{$s} = {
                        kind        => "entry",
                        symbol      => $s,
                        fill_time   => $nxt,
                        use_open    => 1,
                        reason      => "dip_prior_ret_le_-2pct",
                        signal_open => $dbar->{o},
                    };
                }
            }
        }

        my $eq = equity_at($cash, \%positions, $h1, \%idx, $t, "close");
        push @equity_curve, [ $t, $eq ];
        push @{ $daily_equity{ ymd($t) } }, [ $t, $eq ];
    }

    # flatten leftovers on last in-window close
    for my $s (keys %positions) {
        my $last_bar;
        if (exists $idx{$s}{$last_t}) {
            $last_bar = $h1->{$s}[ $idx{$s}{$last_t} ];
        } else {
            my @cands = grep { $WINDOW_START <= $_->{ot} && $_->{ot} <= $WINDOW_END } @{ $h1->{$s} };
            next unless @cands;
            $last_bar = $cands[-1];
        }
        $close_pos->($s, $last_bar->{ct}, $last_bar->{c}, "flatten_last_close");
        push @notes, "flattened open $s at last closed bar " . iso($last_bar->{ct});
    }

    my $ending = $cash;
    my @days;
    my @dates = sort keys %daily_equity;
    my $prev_end = STARTING_BALANCE;
    for my $d (@dates) {
        my $marks = $daily_equity{$d};
        my $start_eq = $prev_end;
        my $end_eq = $marks->[-1][1];
        $end_eq = $ending if $d eq $dates[-1];
        my $pnl = $end_eq - $start_eq;
        push @days, {
            date             => $d,
            starting_equity  => $start_eq,
            ending_equity    => $end_eq,
            pnl              => $pnl,
            trades           => $daily_trade_count{$d} || 0,
            fees_paid        => $daily_fees{$d} || 0.0,
        };
        $prev_end = $end_eq;
    }

    my @pnls = map { $_->{pnl} } @days;
    my $win_days  = scalar grep { $_ > 0 } @pnls;
    my $lose_days = scalar grep { $_ < 0 } @pnls;
    my ($best, $worst);
    if (@days) {
        $best = $days[0];
        $worst = $days[0];
        for my $r (@days) {
            $best = $r if $r->{pnl} > $best->{pnl};
            $worst = $r if $r->{pnl} < $worst->{pnl};
        }
    }
    my $n_wins = scalar grep { $_->{pnl} > 0 } @trades;
    my $peak = STARTING_BALANCE;
    my $max_dd = 0.0;
    my $max_dd_usd = 0.0;
    for my $pt (@equity_curve) {
        my $eq = $pt->[1];
        $peak = $eq if $eq > $peak;
        my $dd_usd = $peak - $eq;
        my $dd = $peak > 0 ? $dd_usd / $peak : 0.0;
        if ($dd > $max_dd) {
            $max_dd = $dd;
            $max_dd_usd = $dd_usd;
        }
    }
    if ($ending < $peak) {
        my $dd_usd = $peak - $ending;
        my $dd = $peak > 0 ? $dd_usd / $peak : 0.0;
        if ($dd > $max_dd) {
            $max_dd = $dd;
            $max_dd_usd = $dd_usd;
        }
    }
    my $avg = 0.0;
    if (@pnls) {
        my $sum = 0;
        $sum += $_ for @pnls;
        $avg = $sum / @pnls;
    }
    push @notes, sprintf(
        "avg_day_pnl=\$%.2f on \$%.0f over %d calendar days. Rules were not tweaked to force a win.",
        $avg, STARTING_BALANCE, scalar(@days)
    );

    return {
        strategy          => $strategy,
        book              => $book,
        start             => iso($first_t),
        end               => iso($last_t),
        start_epoch       => $first_t,
        end_epoch         => $last_t,
        starting_balance  => STARTING_BALANCE,
        ending_equity     => $ending,
        total_pnl         => $ending - STARTING_BALANCE,
        total_return      => ($ending - STARTING_BALANCE) / STARTING_BALANCE,
        n_trades          => scalar(@trades),
        n_wins            => $n_wins,
        win_rate          => @trades ? $n_wins / @trades : 0.0,
        win_days          => $win_days,
        lose_days         => $lose_days,
        avg_day_pnl       => $avg,
        best_day          => { date => $best ? $best->{date} : undef, pnl => $best ? $best->{pnl} : 0.0 },
        worst_day         => { date => $worst ? $worst->{date} : undef, pnl => $worst ? $worst->{pnl} : 0.0 },
        max_drawdown      => $max_dd,
        max_drawdown_usd  => $max_dd_usd,
        fees_paid         => $fees_paid,
        days              => \@days,
        trades            => \@trades,
        notes             => \@notes,
        data_source       => $data_source,
        symbols_used      => \@symbols,
        symbols_skipped   => $skipped,
        allocation        => $allocation,
        fee_assumption    => "10 bps taker + 1 bp half-spread each side (12 bps one-way, 24 bps round trip); fill at OHLC, spread in fee",
        lookahead         => "daily: signal on UTC daily close, fill next day's open; dip_hold recovery/time-stop exit on that daily close",
        paper_only        => JSON::PP::true,
        live_orders       => JSON::PP::false,
        n_days            => scalar(@days),
        rules_fixed       => JSON::PP::true,
        curve_fit         => JSON::PP::false,
    };
}

sub raw_buy_hold {
    my ($h1) = @_;
    my %out;
    for my $s (keys %$h1) {
        my @in = grep { $WINDOW_START <= $_->{ot} && $_->{ot} <= $WINDOW_END } @{ $h1->{$s} };
        next unless @in;
        my $first = $in[0];
        my $last  = $in[-1];
        my $ret = $first->{o} > 0 ? $last->{c} / $first->{o} - 1.0 : 0.0;
        $out{$s} = {
            first_open_time => iso($first->{ot}),
            first_open      => $first->{o},
            last_close_time => iso($last->{ct}),
            last_close      => $last->{c},
            return          => $ret,
            return_pct      => $ret * 100.0,
            fees            => 0.0,
            note            => "raw first-open to last-close, no fees, no book",
        };
    }
    return \%out;
}

sub monthly_pnl {
    my ($result) = @_;
    my %buckets;
    my %n_days;
    for my $row (@{ $result->{days} }) {
        my $ym = substr($row->{date}, 0, 7);
        $buckets{$ym} += $row->{pnl};
        $n_days{$ym}++;
    }
    return [ map { { month => $_, pnl => $buckets{$_}, n_days => $n_days{$_} } } sort keys %buckets ];
}

sub save_result {
    my ($result, $stamp) = @_;
    my $sd = substr($result->{start}, 0, 10);
    my $ed = substr($result->{end}, 0, 10);
    my $tag = sprintf(
        "swing_%s_%s_%s_%s_%d_%s",
        $result->{strategy}, $result->{book}, $sd, $ed,
        int($result->{starting_balance}), $stamp
    );
    my $json_path = "$OUT_DIR/${tag}.json";
    my $csv_path  = "$OUT_DIR/${tag}_daily.csv";
    my $payload = { %$result };
    # drop internal epochs
    delete $payload->{start_epoch};
    delete $payload->{end_epoch};
    open my $jh, ">", $json_path or die $!;
    print $jh JSON::PP->new->canonical(0)->pretty->encode($payload);
    close $jh;
    open my $ch, ">", $csv_path or die $!;
    print $ch "date,starting_equity,ending_equity,pnl,trades,fees_paid\n";
    for my $d (@{ $result->{days} }) {
        printf $ch "%s,%.6f,%.6f,%.6f,%d,%.6f\n",
            $d->{date}, $d->{starting_equity}, $d->{ending_equity},
            $d->{pnl}, $d->{trades}, $d->{fees_paid};
    }
    close $ch;
    return ($json_path, $csv_path);
}

sub print_table {
    my (@results) = @_;
    my @headers = qw(strategy book start end end_eq pnl ret% trades win% avg_day best worst maxDD% maxDD$ fees);
    my @rows;
    for my $r (@results) {
        push @rows, [
            $r->{strategy},
            $r->{book},
            substr($r->{start}, 0, 10),
            substr($r->{end}, 0, 10),
            sprintf("%.2f", $r->{ending_equity}),
            sprintf("%+.2f", $r->{total_pnl}),
            sprintf("%+.2f", $r->{total_return} * 100),
            $r->{n_trades},
            sprintf("%.1f", $r->{win_rate} * 100),
            sprintf("%+.2f", $r->{avg_day_pnl}),
            sprintf("%+.2f", $r->{best_day}{pnl}),
            sprintf("%+.2f", $r->{worst_day}{pnl}),
            sprintf("%.2f", $r->{max_drawdown} * 100),
            sprintf("%.2f", $r->{max_drawdown_usd}),
            sprintf("%.2f", $r->{fees_paid}),
        ];
    }
    my @w = map { length($_) } @headers;
    for my $row (@rows) {
        for my $i (0 .. $#$row) {
            $w[$i] = length($row->[$i]) if length($row->[$i]) > $w[$i];
        }
    }
    my $fmt = sub {
        my (@c) = @_;
        my @out;
        for my $i (0 .. $#c) {
            if ($i <= 3) { push @out, sprintf("%-*s", $w[$i], $c[$i]); }
            else         { push @out, sprintf("%*s", $w[$i], $c[$i]); }
        }
        return join("  ", @out);
    };
    print "\n";
    print "PAPER dip_hold  \$10,000  ~6m  no live orders  no leverage  rule NOT retuned\n";
    print $fmt->(@headers), "\n";
    print join("  ", map { "-" x $_ } @w), "\n";
    print $fmt->(@$_), "\n" for @rows;
    print "\n";
    print "Fees: 12 bps one-way (10 taker + 1 half-spread) each side. Long-only.\n";
    print "Signal on UTC daily close, fill next day's open. Exit on close.\n";
    print "dip_hold: prior daily ret <= -2%, hold 3 daily closes or recover\n";
    print "above the signal day's open, whichever first. No stacking.\n";
}

# --- optional 30d self-check against known paper result ---
sub load_cached_1h {
    my ($path) = @_;
    open my $fh, "<", $path or die "cache $path: $!";
    local $/;
    my $payload = JSON::PP->new->decode(<$fh>);
    close $fh;
    my %h1;
    for my $s (keys %{ $payload->{bars_1h} }) {
        my @bars;
        for my $row (@{ $payload->{bars_1h}{$s} }) {
            push @bars, {
                ot => parse_iso($row->{open_time}),
                ct => parse_iso($row->{close_time}),
                o  => 0 + $row->{open},
                h  => 0 + $row->{high},
                l  => 0 + $row->{low},
                c  => 0 + $row->{close},
                v  => 0 + $row->{volume},
            };
        }
        @bars = sort { $a->{ot} <=> $b->{ot} } @bars;
        $h1{$s} = \@bars;
    }
    return \%h1;
}

# --- main ---
my $now = time;
my $stamp = strftime("%Y%m%dT%H%M%SZ", gmtime($now));
print "swing 6m paper backtest  window ", iso($WINDOW_START), " → ", iso($WINDOW_END),
      "  warmup from ", iso($WARMUP_START), "  now=", iso($now), "\n";
print "Honesty: paper only, no live orders, no lookahead, dip_hold -2% / 3-close rule copied from 30d test, not retuned.\n";

# Verify port against 30d cache if present (does not change the 6m rule).
my $cache30 = "/workspace/crypto-bot/data/backtests/swing_klines_20260815T101154Z.json";
if (-f $cache30) {
    my $saved_ws = $WINDOW_START;
    my $saved_we = $WINDOW_END;
    $WINDOW_START = timegm(0, 0, 10, 16, 6, 2026);  # 2026-07-16 10:00Z
    $WINDOW_END   = timegm(0, 0,  9, 15, 7, 2026);  # 2026-08-15 09:00Z
    my $h30 = load_cached_1h($cache30);
    my %d30 = map { $_ => resample_daily($h30->{$_}) } keys %$h30;
    my $r_btc = simulate_book("dip_hold", "btc100", $h30, \%d30, $VISION, []);
    my $r_f25 = simulate_book("dip_hold", "four25", $h30, \%d30, $VISION, []);
    printf "30d self-check btc100 pnl=%+.4f (expect +213.6246) trades=%d\n",
        $r_btc->{total_pnl}, $r_btc->{n_trades};
    printf "30d self-check four25 pnl=%+.4f (expect +400.8546) trades=%d\n",
        $r_f25->{total_pnl}, $r_f25->{n_trades};
    $WINDOW_START = $saved_ws;
    $WINDOW_END   = $saved_we;
}

my %h1;
my @sources;
my @skipped;
my $data_source = $VISION;

for my $sym (@SYMBOLS) {
    eval {
        my ($bars, $src) = fetch_klines($sym, $WARMUP_START, $WINDOW_END);
        $h1{$sym} = $bars;
        $data_source = $src;
        my $msg = sprintf(
            "%s 1h %s n=%d first=%s last=%s",
            $sym, $src, scalar(@$bars), iso($bars->[0]{ot}), iso($bars->[-1]{ot})
        );
        push @sources, $msg;
        print "$msg\n";
        1;
    } or do {
        my $err = $@ || "unknown";
        chomp $err;
        push @skipped, "$sym: $err";
        print "SKIP $sym: $err\n";
    };
}

die "ERROR: no BTCUSDT klines; refusing to invent fills/PnL\n" unless $h1{BTCUSDT};

for my $sym (keys %h1) {
    my @in = grep { $WINDOW_START <= $_->{ot} && $_->{ot} <= $WINDOW_END } @{ $h1{$sym} };
    if (!@in) {
        push @skipped, "$sym: no in-window bars";
        delete $h1{$sym};
        next;
    }
    printf "%s window_n=%d first=%s last=%s\n",
        $sym, scalar(@in), iso($in[0]{ot}), iso($in[-1]{ot});
}

my %daily;
for my $s (keys %h1) {
    $daily{$s} = resample_daily($h1{$s});
    my $series = $daily{$s};
    my $complete = scalar grep { $_->{complete} } @$series;
    my $pre = scalar grep { $_->{complete} && $_->{ot} < $WINDOW_START } @$series;
    printf "%s daily n=%d complete=%d complete_before_window=%d first=%s last=%s last_complete=%s last_hours=%d\n",
        $s, scalar(@$series), $complete, $pre, $series->[0]{date}, $series->[-1]{date},
        $series->[-1]{complete} ? "true" : "false", $series->[-1]{n_hours};
    if ($pre < 1) {
        push @skipped, "$s: no complete UTC day before window (first dip signal needs prior close)";
    }
}

# persist klines
my %bars_1h;
for my $s (keys %h1) {
    $bars_1h{$s} = [
        map {
            {
                open_time  => iso($_->{ot}),
                close_time => iso($_->{ct}),
                open       => $_->{o},
                high       => $_->{h},
                low        => $_->{l},
                close      => $_->{c},
                volume     => $_->{v},
            }
        } @{ $h1{$s} }
    ];
}
my $merged = {
    fetched_at            => iso($now),
    window_start          => iso($WINDOW_START),
    window_end_bar_open   => iso($WINDOW_END),
    warmup_start          => iso($WARMUP_START),
    horizon               => "6m",
    strategy_filter       => ["dip_hold"],
    sources               => \@sources,
    skipped               => \@skipped,
    bars_1h               => \%bars_1h,
};
my $merged_path = "$OUT_DIR/swing_6m_klines_${stamp}.json";
open my $mh, ">", $merged_path or die $!;
print $mh JSON::PP->new->encode($merged);
close $mh;
print "wrote merged kline cache $merged_path\n";

my $raw = raw_buy_hold(\%h1);
print "raw buy-hold (first open → last close, no fees):\n";
for my $s (@SYMBOLS) {
    next unless $raw->{$s};
    printf "  %-10s  %+.3f%%  %s → %s\n",
        $s, $raw->{$s}{return_pct}, $raw->{$s}{first_open}, $raw->{$s}{last_close};
}

my @results;
my @files;
my %monthly;
for my $book (qw(btc100 four25)) {
    if ($book eq "btc100" && !$h1{BTCUSDT}) {
        print "SKIP dip_hold $book: no BTCUSDT\n";
        next;
    }
    my $res = simulate_book("dip_hold", $book, \%h1, \%daily, $data_source, \@skipped);
    my ($jp, $cp) = save_result($res, $stamp);
    push @results, $res;
    push @files, { strategy => "dip_hold", book => $book, json => $jp, csv => $cp };
    $monthly{"dip_hold_$book"} = monthly_pnl($res);
    printf "dip_hold         %-8s end=\$%.2f pnl=%+.2f trades=%d avg_day=%+.2f -> %s\n",
        $book, $res->{ending_equity}, $res->{total_pnl}, $res->{n_trades},
        $res->{avg_day_pnl}, $jp;
}

print_table(@results);

print "monthly PnL (sum of daily marked PnL):\n";
for my $key (sort keys %monthly) {
    print "  $key\n";
    for my $row (@{ $monthly{$key} }) {
        printf "    %s  %+.2f  (%d days)\n", $row->{month}, $row->{pnl}, $row->{n_days};
    }
}

my ($f25) = grep { $_->{book} eq "four25" } @results;
my ($btc) = grep { $_->{book} eq "btc100" } @results;
my $cmp_line;
if ($f25 && $btc) {
    $cmp_line = sprintf(
        "vs 30d (same rule, not retuned): 6m four25 %+.2f vs 30d +%.2f; 6m btc100 %+.2f vs 30d +%.2f (2026-07-16 to 2026-08-15).",
        $f25->{total_pnl}, $REF_30D_F25, $btc->{total_pnl}, $REF_30D_BTC
    );
} else {
    $cmp_line = "vs 30d: missing a 6m book; 30d was four25 +\$400.85, btc100 +\$213.62.";
}
print "$cmp_line\n";

my $extra = {
    fetched_at            => iso($now),
    horizon               => "6m",
    window_start          => iso($WINDOW_START),
    window_end_bar_open   => iso($WINDOW_END),
    last_closed_hour      => iso($WINDOW_END),
    warmup_start          => iso($WARMUP_START),
    data_source           => $data_source,
    sources               => \@sources,
    skipped               => \@skipped,
    kline_cache           => $merged_path,
    fee_assumption        => "10 bps taker + 1 bp half-spread each side (12 bps one-way)",
    starting_balance      => STARTING_BALANCE + 0,
    leverage              => 1.0,
    paper_only            => JSON::PP::true,
    live_orders           => JSON::PP::false,
    curve_fit             => JSON::PP::false,
    parameters_optimized_after_results => JSON::PP::false,
    rule_retuned          => JSON::PP::false,
    dip_ret               => DIP_RET + 0,
    dip_hold_closes       => DIP_HOLD_CLOSES + 0,
    honesty =>
        "Paper only. No live orders. No lookahead. dip_hold rule matches the 30d test exactly (prior daily ret <= -2%, exit after 3 daily closes or close above signal-day open, long-only, no stacking). Not retuned to win. PnL from public OHLC fills only.",
    raw_buy_hold_no_fees  => $raw,
    monthly_pnl           => \%monthly,
    vs_30d => {
        window  => "2026-07-16 to 2026-08-15",
        four25  => $REF_30D_F25,
        btc100  => $REF_30D_BTC,
        line    => $cmp_line,
    },
    books_summary => [
        map {
            {
                strategy         => $_->{strategy},
                book             => $_->{book},
                start            => $_->{start},
                end              => $_->{end},
                ending_equity    => $_->{ending_equity},
                total_pnl        => $_->{total_pnl},
                total_return     => $_->{total_return},
                n_trades         => $_->{n_trades},
                win_rate         => $_->{win_rate},
                avg_day_pnl      => $_->{avg_day_pnl},
                best_day         => $_->{best_day},
                worst_day        => $_->{worst_day},
                max_drawdown     => $_->{max_drawdown},
                max_drawdown_usd => $_->{max_drawdown_usd},
                fees_paid        => $_->{fees_paid},
            }
        } @results
    ],
    books => [ map { my $c = { %$_ }; delete $c->{start_epoch}; delete $c->{end_epoch}; $c } @results ],
    files => \@files,
};
my $cmp_path = "$OUT_DIR/swing_6m_dip_hold_comparison_${stamp}.json";
open my $xh, ">", $cmp_path or die $!;
print $xh JSON::PP->new->pretty->encode($extra);
close $xh;
print "comparison json: $cmp_path\n";
for my $f (@files) {
    printf "  %-16s %-8s  %s\n", $f->{strategy}, $f->{book}, $f->{json};
    printf "  %-16s %-8s  %s\n", "", "", $f->{csv};
}
if (@skipped) {
    print "skipped / warnings:\n";
    print "  - $_\n" for @skipped;
}
exit 0;
