
drop table if exists articles cascade;
drop table if exists sources cascade;
drop table if exists scraper_logs;

begin;

create table sources (
    id serial primary key,
    name varchar(100) unique not null,
    base_url text,
    source_type varchar(30),
    created_at timestamp default now()
);

create table articles (
    id bigserial primary key,
    source_id int references sources(id) on delete cascade,
    title text not null,
    content text not null,
    url text unique not null,
    published_at timestamp,
    scraped_at timestamp default now(),
    hash varchar(64) unique
);

create table if not exists scraper_logs (
    id bigserial primary key,
    timestamp timestamp default now(),
    timezone varchar(10) not null,
    level varchar(20) not null,
    source varchar(100) not null,
    message text not null,
    details jsonb,
    created_at timestamp default now()
);

create index idx_articles_source_id on articles(source_id);
create index idx_articles_published_at on articles(published_at DESC);
create index idx_articles_scraped_at on articles(scraped_at DESC);
create index idx_articles_hash on articles(hash);
create index idx_articles_url on articles(url);

create index idx_scraper_logs_timestamp on scraper_logs(timestamp desc);
create index idx_scraper_logs_source on scraper_logs(source);
create index idx_scraper_logs_level on scraper_logs(level);

create index idx_articles_source_published on articles(source_id, published_at DESC);

commit;