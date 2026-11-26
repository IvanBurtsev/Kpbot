"""Мастер 1С: определение конфигурации по метаданным и сохранение настроек."""

import support  # общий стенд: изолированные данные, сеть запрещена

import json
import unittest
from pathlib import Path

import httpx

from kpbot.one_c import OneCClient
from kpbot.one_c_profiles import detect_configuration, detect_features, parse_entities

METADATA_UT = """<?xml version="1.0"?>
<edmx:Edmx xmlns:edmx="http://schemas.microsoft.com/ado/2007/06/edmx">
  <edmx:DataServices>
    <Schema Namespace="StandardODATA">
      <EntityContainer>
        <EntitySet Name="Catalog_Номенклатура" EntityType="StandardODATA.Catalog_Номенклатура"/>
        <EntitySet Name="Catalog_КлассификаторЕдиницИзмерения"/>
        <EntitySet Name="Catalog_Склады"/>
        <EntitySet Name="Catalog_Контрагенты"/>
        <EntitySet Name="InformationRegister_ЦеныНоменклатуры"/>
        <EntitySet Name="AccumulationRegister_ТоварыНаСкладах"/>
        <EntitySet Name="Document_ЗаказКлиента"/>
        <EntitySet Name="Document_РеализацияТоваровУслуг"/>
      </EntityContainer>
    </Schema>
  </edmx:DataServices>
</edmx:Edmx>"""

METADATA_BP = """<?xml version="1.0"?>
<edmx:Edmx xmlns:edmx="http://schemas.microsoft.com/ado/2007/06/edmx">
  <edmx:DataServices>
    <Schema Namespace="StandardODATA">
      <EntityContainer>
        <EntitySet Name="Catalog_Номенклатура"/>
        <EntitySet Name="Catalog_Склады"/>
        <EntitySet Name="Catalog_Контрагенты"/>
        <EntitySet Name="Document_СчетНаОплатуПокупателю"/>
        <EntitySet Name="Document_ПоступлениеТоваровУслуг"/>
      </EntityContainer>
    </Schema>
  </edmx:DataServices>
</edmx:Edmx>"""


class ProfileTest(unittest.TestCase):
    def test_parse_entities(self) -> None:
        names = parse_entities(METADATA_UT)
        self.assertIn("Catalog_Номенклатура", names)
        self.assertIn("Document_ЗаказКлиента", names)

    def test_ut_is_detected(self) -> None:
        report = detect_configuration(parse_entities(METADATA_UT))
        self.assertIn("УТ", report["configuration"])
        self.assertTrue(report["features"]["orders"])
        self.assertTrue(report["features"]["stock_register"])
        self.assertTrue(report["supported_write"])

    def test_bukhgalteria_is_detected(self) -> None:
        report = detect_configuration(parse_entities(METADATA_BP))
        self.assertIn("Бухгалтерия", report["configuration"])
        self.assertFalse(report["features"]["orders"])
        self.assertTrue(report["features"]["invoice"])

    def test_unknown_is_honest(self) -> None:
        report = detect_configuration({"SomeEntity"})
        self.assertIn("неизвестная", report["configuration"])
        self.assertFalse(any(report["features"].values()))


class DiagnoseTest(unittest.TestCase):
    def _client(self, metadata: str, prices: bool = True) -> OneCClient:
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/$metadata"):
                return httpx.Response(200, text=metadata)
            if "Catalog_КлассификаторЕдиницИзмерения" in path:
                return httpx.Response(200, json={"value": [{"Ref_Key": "u1", "Description": "шт"}]})
            if "Catalog_Номенклатура" in path:
                return httpx.Response(200, json={"value": [
                    {"Ref_Key": "n1", "Артикул": "KR-001", "Description": "Уголок", "ЕдиницаИзмерения_Key": "u1"}
                ]})
            if "Catalog_Склады" in path:
                return httpx.Response(200, json={"value": [{"Ref_Key": "w1", "Description": "Основной"}]})
            if "InformationRegister_ЦеныНоменклатуры" in path:
                if not prices:
                    return httpx.Response(200, json={"value": []})
                return httpx.Response(200, json={"value": [{"Номенклатура_Key": "n1", "Цена": 10}]})
            if "AccumulationRegister" in path:
                return httpx.Response(404)
            return httpx.Response(404)

        http = httpx.Client(transport=httpx.MockTransport(handler), timeout=5)
        return OneCClient("https://1c.example/base", "u", "p", http=http)

    def test_ut_report(self) -> None:
        report = self._client(METADATA_UT).diagnose()
        self.assertIn("УТ", report["configuration"])
        self.assertEqual(report["entities_total"], 8)
        self.assertTrue(report["reads"]["nomenclature"]["ok"])
        self.assertTrue(report["reads"]["prices"]["ok"])
        self.assertFalse(report["reads"]["stock"]["ok"])

    def test_bp_has_no_orders(self) -> None:
        report = self._client(METADATA_BP, prices=False).diagnose()
        self.assertIn("Бухгалтерия", report["configuration"])
        self.assertFalse(report["features"]["orders"])
        self.assertIn("нет цен", report["reads"]["prices"]["detail"])


class EnvSaveTest(unittest.TestCase):
    def test_updates_only_one_c_keys(self) -> None:
        from kpbot.cli import _save_one_c_env

        path = Path(support.ROOT) / "env-test"
        path.write_text("KPBOT_ENV=dev\nKPBOT_ONE_C_USER=old\n", encoding="utf-8")
        _save_one_c_env(
            {"KPBOT_ONE_C_BASE_URL": "https://1c/base", "KPBOT_ONE_C_USER": "new"},
            path=path,
        )
        text = path.read_text(encoding="utf-8")
        self.assertIn("KPBOT_ENV=dev", text)
        self.assertIn("KPBOT_ONE_C_USER=new", text)
        self.assertNotIn("old", text)
        self.assertIn("KPBOT_ONE_C_BASE_URL=https://1c/base", text)


if __name__ == "__main__":
    unittest.main()
