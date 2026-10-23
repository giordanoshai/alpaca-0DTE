-- Supabase / PostgreSQL
-- ttt_template_store：一行一份完整配置。执行前会删除旧单行 JSONB 表及其数据。

begin;

drop table if exists public.ttt_template_store;

create table public.ttt_template_store (
    id smallint primary key,
    name text not null unique
        check (length(btrim(name)) between 1 and 80),
    payload jsonb not null
        check (
            jsonb_typeof(payload) = 'object'
            and jsonb_typeof(payload -> 'params') = 'object'
            and payload ->> 'status' in ('active', 'pending', 'saved')
        ),
    updated_at timestamptz not null default now()
);

-- 首条配置必须由数据库明确写入；应用不会自动生成“默认/保守/激进”。
-- 下面保留你当前设置页保存的“测试”配置，并将它设为 active。
insert into public.ttt_template_store (id, name, payload)
values (
    1,
    '测试',
    jsonb_build_object(
        'params', jsonb_build_object(
            'max_daily_loss', '15000',
            'max_weekly_loss', '300000',
            'max_position_size', '500',
            'opening_blackout_min', '10',
            'min_delta', '0.40',
            'max_spread_pct', '0.03',
            'stop_pct', '0.5',
            'max_loss_per_trade', '100',
            'max_hold_mins', '5',
            'cooldown_seconds', 6,
            'chase_sigma_k', '1.5',
            'position_tolerance', '0.20',
            'max_trades_per_day', 50,
            'daily_profit_target', '500000',
            'wall_room_atr_threshold', '3.0',
            'entry_cutoff_mins', '150',
            'order_timeout_s', '5'
        ),
        'reason', 'ceshi',
        'status', 'active',
        'created_at', now()
    )
);

grant select, insert, update, delete
    on table public.ttt_template_store
    to readonly_user;

commit;
