from __future__ import annotations

from unittest import TestCase

from pydantic import ValidationError

from app.evidence.kubernetes import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    KubernetesQuery,
    ResourceKind,
    describe_query,
    request_path,
)

NS = "faultweld-demo"


class QueryValidationTests(TestCase):
    def test_valid_queries_are_accepted(self) -> None:
        KubernetesQuery(kind=ResourceKind.DEPLOYMENT, name="faultweld-sample")
        KubernetesQuery(kind=ResourceKind.POD)
        for selector in (
            "app=x",
            "app.kubernetes.io/name=faultweld-sample",
            "a=b,c!=d",
            "a",
            "!a",
            "a==b",
            "a=",
        ):
            with self.subTest(selector=selector):
                KubernetesQuery(kind=ResourceKind.POD, label_selector=selector)

    def test_default_and_limit_bounds(self) -> None:
        self.assertEqual(KubernetesQuery(kind=ResourceKind.POD).limit, DEFAULT_LIMIT)
        KubernetesQuery(kind=ResourceKind.POD, limit=1)
        KubernetesQuery(kind=ResourceKind.POD, limit=MAX_LIMIT)
        for limit in (0, -1, MAX_LIMIT + 1):
            with self.subTest(limit=limit), self.assertRaises(ValidationError):
                KubernetesQuery(kind=ResourceKind.POD, limit=limit)

    def test_invalid_names_are_rejected(self) -> None:
        for name in (
            "",
            "Upper",
            "a/b",
            "a b",
            "-a",
            "a-",
            "../x",
            "a?b=c",
            "a%2fb",
            "a\n",
            "x" * 101,
        ):
            with self.subTest(name=name), self.assertRaises(ValidationError):
                KubernetesQuery(kind=ResourceKind.DEPLOYMENT, name=name)

    def test_invalid_selectors_are_rejected(self) -> None:
        for selector in (
            "",
            "a in (b,c)",
            "a notin (b)",
            "a=b&x=y",
            "a=b;c",
            "a=b,",
            ",a=b",
            "a=b c",
            "=b",
            "a=-b",
            "a=b/c",
            "a==b==c",
            "a!==b",
            "k/y/z=v",
            "Foo/bar=v",
            "!",
            "!!a",
            "a=b\n",
            ",".join(f"k{index}=v" for index in range(9)),
            "a=" + "x" * 64,
        ):
            with self.subTest(selector=selector), self.assertRaises(ValidationError):
                KubernetesQuery(kind=ResourceKind.POD, label_selector=selector)

    def test_name_and_selector_are_mutually_exclusive(self) -> None:
        with self.assertRaises(ValidationError):
            KubernetesQuery(kind=ResourceKind.POD, name="a", label_selector="a=b")

    def test_unknown_kinds_are_rejected(self) -> None:
        for kind in ("secret", "configmap", "node", "namespace", "pods/exec", ""):
            with self.subTest(kind=kind), self.assertRaises(ValidationError):
                KubernetesQuery.model_validate({"kind": kind})

    def test_a_namespace_cannot_be_supplied(self) -> None:
        for field in ("namespace", "cluster", "path"):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                KubernetesQuery.model_validate({"kind": "pod", field: "kube-system"})


class RequestPathTests(TestCase):
    def test_named_reads(self) -> None:
        self.assertEqual(
            request_path(KubernetesQuery(kind=ResourceKind.DEPLOYMENT, name="faultweld-sample")),
            f"/apis/apps/v1/namespaces/{NS}/deployments/faultweld-sample",
        )
        self.assertEqual(
            request_path(KubernetesQuery(kind=ResourceKind.SERVICE, name="faultweld-sample")),
            f"/api/v1/namespaces/{NS}/services/faultweld-sample",
        )

    def test_list_reads_are_bounded_and_selectors_are_encoded(self) -> None:
        self.assertEqual(
            request_path(KubernetesQuery(kind=ResourceKind.POD)),
            f"/api/v1/namespaces/{NS}/pods?limit=20",
        )
        self.assertEqual(
            request_path(
                KubernetesQuery(
                    kind=ResourceKind.POD,
                    label_selector="app.kubernetes.io/name=faultweld-sample",
                    limit=5,
                )
            ),
            f"/api/v1/namespaces/{NS}/pods?limit=5"
            "&labelSelector=app.kubernetes.io%2Fname%3Dfaultweld-sample",
        )

    def test_every_path_stays_in_the_project_namespace(self) -> None:
        for kind in ResourceKind:
            for query in (
                KubernetesQuery(kind=kind),
                KubernetesQuery(kind=kind, name="x"),
                KubernetesQuery(kind=kind, label_selector="a=b"),
            ):
                with self.subTest(query=query):
                    self.assertIn(f"/namespaces/{NS}/", request_path(query))

    def test_query_descriptions(self) -> None:
        self.assertEqual(
            describe_query(KubernetesQuery(kind=ResourceKind.DEPLOYMENT, name="faultweld-sample")),
            "GET apis/apps/v1/deployments namespace=faultweld-demo name=faultweld-sample",
        )
        self.assertEqual(
            describe_query(
                KubernetesQuery(kind=ResourceKind.POD, label_selector="app=x", limit=5)
            ),
            "GET api/v1/pods namespace=faultweld-demo selector=app=x limit=5",
        )
