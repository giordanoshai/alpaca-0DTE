-- 本程序逐笔订单审计表。
-- ttt_trades 继续负责“一次持仓生命周期一行”；本表负责“每个 Alpaca order_id 一行”，
-- 保存开仓/加仓/减仓/全平、触发原因、撤单发起者和券商终态，互不覆盖。

begin;

create table if not exists public.ttt_order_events (
    order_id text primary key,
    parent_order_id text,
    client_order_id text,
    ticker text not null,
    option_code text not null,
    side text not null check (side in ('buy', 'sell')),
    qty integer not null check (qty >= 0),
    status text not null check (status in ('pending', 'filled', 'cancelled')),
    origin text not null,
    action text not null,
    reason text,
    cancel_origin text,
    fail_reason text,
    broker_status text,
    price numeric,
    price_type text,
    limit_price numeric,
    submitted_at timestamptz not null,
    updated_at timestamptz not null,
    created_at timestamptz not null default now()
);

create index if not exists ttt_order_events_submitted_idx
    on public.ttt_order_events (submitted_at desc);

create index if not exists ttt_order_events_parent_idx
    on public.ttt_order_events (parent_order_id);

create index if not exists ttt_order_events_status_idx
    on public.ttt_order_events (status, updated_at desc);

grant select, insert, update
    on table public.ttt_order_events
    to readonly_user;

commit;
