#!/usr/bin/env python3
import argparse
import json
import sys

from config import KEYWORDS_FILE, ensure_data_dir
from pipeline.orchestrator import (
    collect_products,
    generate_posts,
    load_keywords,
    publish_posts,
    run_full_pipeline,
    sync_reports,
)
from reports.export import export_reports_to_excel
from storage.db import Database


def cmd_search(args: argparse.Namespace) -> None:
    keywords = [args.keyword] if args.keyword else load_keywords()
    count = collect_products(keywords, args.limit)
    print(f"수집 완료: {count}개 상품 저장")


def cmd_generate(args: argparse.Namespace) -> None:
    count = generate_posts(args.limit)
    print(f"글 생성 완료: {count}개")


def cmd_post(args: argparse.Namespace) -> None:
    count = publish_posts(args.limit)
    print(f"워드프레스 업로드 완료: {count}개")


def cmd_report(args: argparse.Namespace) -> None:
    counts = sync_reports(days=args.days)
    path = export_reports_to_excel()
    print(f"리포트 저장: {counts}")
    print(f"엑셀 파일: {path}")


def cmd_run(args: argparse.Namespace) -> None:
    result = run_full_pipeline(
        collect_limit=args.limit,
        generate_limit=args.limit,
        publish_limit=args.limit,
        report_days=args.days,
        skip_publish=args.skip_publish,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_status(_: argparse.Namespace) -> None:
    summary = Database().dashboard_summary()
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="쿠팡파트너스 올인원 자동화")
    sub = parser.add_subparsers(dest="command", required=True)

    search = sub.add_parser("search", help="키워드로 상품 수집")
    search.add_argument("--keyword", help="단일 키워드 (미입력 시 keywords.txt)")
    search.add_argument("--limit", type=int, default=3, help="키워드당 상품 수")
    search.set_defaults(func=cmd_search)

    generate = sub.add_parser("generate", help="수집 상품 AI 글 생성")
    generate.add_argument("--limit", type=int, default=5)
    generate.set_defaults(func=cmd_generate)

    post = sub.add_parser("post", help="생성 글 워드프레스 업로드")
    post.add_argument("--limit", type=int, default=5)
    post.set_defaults(func=cmd_post)

    report = sub.add_parser("report", help="수익/클릭 리포트 수집 + 엑셀")
    report.add_argument("--days", type=int, default=7)
    report.set_defaults(func=cmd_report)

    run = sub.add_parser("run", help="전체 파이프라인 실행")
    run.add_argument("--limit", type=int, default=3)
    run.add_argument("--days", type=int, default=7)
    run.add_argument("--skip-publish", action="store_true", help="워드프레스 업로드 생략")
    run.set_defaults(func=cmd_run)

    status = sub.add_parser("status", help="현재 DB 상태 요약")
    status.set_defaults(func=cmd_status)

    return parser


def main() -> int:
    ensure_data_dir()
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.func(args)
        return 0
    except Exception as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
