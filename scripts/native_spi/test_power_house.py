"""Normal Power House state-machine and evidence failure boundaries."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from .power_house import PowerHouse, PHASES, CLIMATES, active_units, slew_evidence
from .qualification import qualify
from .run import run


def sample(ms, stage, power, active=(0,), heat=True, outdoor=8, return_c=30):
    units, inlet = [], return_c
    for i in (0, 1):
        on = i in active
        outlet = inlet + (2.5 if on else 0)
        units.append({"address":i+1,"requested_level":3 if on else 0,
                      "requested_mode":"heating" if on else "standby","frequency_hz":30 if on else 0,
                      "flow_lph":800 if active else 0,"inlet_c":inlet,"outlet_c":outlet,"defrost":False})
        inlet = outlet
    fw = {"ott_valid":True,"heat":heat,"mode":"CM2 - Heating" if active else "CM1 - Preflow" if heat else "CM0 - Standby",
          "frequency_hz":[u["frequency_hz"] for u in units],"flow_lph":800 if active else 0,
          "odu_flow_lph":[u["flow_lph"] for u in units],"lowflow_fault":False,
          "control":"Power House","demand_source":"external","external_w":power,"request_w":power,
          "room_selected_c":21,"setpoint_selected_c":21,"outside_c":outdoor,
          "water_in_c":[u["inlet_c"] for u in units],"water_out_c":[u["outlet_c"] for u in units]}
    return {"guest_ms":ms,"observed_from_ms":ms,"stage":stage,"demand_w":power,"odus":units,
            "firmware":fw,"outside_c":[outdoor,outdoor],"nominal_hz":[u["frequency_hz"] for u in units],
            "physical_w":[power/len(active) if i in active else 0 for i in (0,1)]}


class NormalPowerHouse(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.fleet=Mock(now_ms=0)
        self.s=PowerHouse(Path(tmp.name)/"heat",self.fleet)
        self.s.refresh=Mock()
        self.get=Mock(return_value=b"{}")

    def full_run(self):
        s=self.s;s.result["stage"]="standby"
        now,rows,prior,request=0,[],{},0
        def feed(active,heat=True):
            nonlocal now, request
            now+=5000;self.fleet.now_ms=now
            item=sample(now,s.result["stage"],s.power,active,heat)
            if not heat:request=0
            elif request < s.power:request=min(s.power,request+15000/480*5)
            else:request=max(s.power,request-15000/180*5)
            item["firmware"]["request_w"]=request
            for i,u in enumerate(item["odus"],1):
                for reg,val in ((3999,2 if i-1 in active else 0),(1999,u["requested_level"])):
                    if prior.get((i,reg))!=val:
                        rows.append({"unit":i,"register":reg,"values":[val],"at_ms":now,"exception":0})
                        prior[(i,reg)]=val
            s.sample=lambda _:item
            s.observe(self.get)
        feed((),False);feed((),False)
        for name,_,expected,_ in PHASES:
            self.assertEqual(s.result["stage"],name)
            if name=="restart":
                for _ in range(49):feed((),True)
            active=() if expected=="off" else (0,1) if expected=="duo" or name=="mid_up" else (1,) if name=="restart" else (0,)
            for _ in range(100):
                feed(active,expected!="off")
                if s.result["stage"]!=name:break
            self.assertNotEqual(s.result["stage"],name)
        self.assertEqual(s.result["stage"],"complete")
        return {"writes":rows}

    def test_complete_normal_ramp_with_midpoint_topology_free_and_owner_change(self):
        report=self.full_run();self.s.finalize(report)
        self.assertTrue(all(self.s.result["checks"].values()),self.s.result["checks"])
        self.assertEqual(self.s.result["phase_results"]["restart"]["owners"],[2])

    def test_constructor_uses_plain_models_and_all_frequency_points(self):
        self.assertFalse(self.s.result["contract"]["response_injection"])
        self.assertFalse(self.s.result["contract"]["frequency_exclusion"])
        self.fleet.set_odu.assert_any_call({"index":1,"profile":"V2 old"})
        self.assertEqual(dict((r[1],r[2]) for r in self.s.settings)["HP1 - Excluded frequency maximum"],0)
        for climate in CLIMATES:
            self.assertIsInstance(CLIMATES[climate]["outdoor_c"],float)

    def test_api_ack_does_not_bypass_effective_demand_filter(self):
        self.s.power=5000
        row=sample(10000,"mid_up",5000)
        row["firmware"]["request_w"]=3000
        self.assertFalse(self.s.phase_valid(row,"either"))
        row["firmware"]["request_w"]=5000
        self.assertTrue(self.s.phase_valid(row,"either"))

    def test_temperature_readback_and_source_must_match_profile(self):
        self.s.power=2600
        row=sample(10000,"low_up",2600)
        self.assertTrue(self.s.phase_valid(row,"single"))
        for key,val in (("outside_c",-5),("water_in_c",[40,32.5]),("control","Water Temperature Control (heating curve)"),
                        ("demand_source","model"),("room_selected_c",19)):
            bad=copy.deepcopy(row);bad["firmware"][key]=val
            self.assertFalse(self.s.phase_valid(bad,"single"),key)

    def test_unknown_frequency_and_mode_wait_instead_of_crashing(self):
        self.s.power=2600
        row=sample(10000,"low_up",2600)
        row["firmware"]["frequency_hz"][0]=None
        self.assertIsNone(active_units(row))
        self.assertFalse(self.s.phase_valid(row,"single"))

    def test_cold_idle_keeps_freeze_circulation_without_starting_compressors(self):
        self.s.climate=dict(CLIMATES["cold"])
        row=sample(10000,"standby",0,(),False,outdoor=-5)
        row["firmware"]["mode"]="CM98 - Anti-Freeze Protection - Water Circulation"
        self.assertFalse(self.s.phase_valid(row,"off"))
        for unit in row["odus"]:unit["flow_lph"]=800
        row["firmware"].update(flow_lph=800,odu_flow_lph=[800,800])
        self.assertTrue(self.s.phase_valid(row,"off"))
        self.s.climate=dict(CLIMATES["mild"])
        row["firmware"]["outside_c"]=8;row["outside_c"]=[8,8]
        self.assertFalse(self.s.phase_valid(row,"off"))

    def test_immediate_large_demand_jumps_cannot_hide_behind_api_ack(self):
        rows=[sample(0,"mid_up",2600),sample(5000,"high",10000)]
        self.assertFalse(slew_evidence(rows)["high"]["plausible"])
        rows=[sample(0,"high",10000),sample(5000,"mid_down",5000)]
        self.assertFalse(slew_evidence(rows)["mid_down"]["plausible"])
        rows=[sample(0,"high",10000),sample(5000,"mid_down",10000-15000/180*30)]
        self.assertTrue(slew_evidence(rows)["mid_down"]["plausible"])
        row=sample(10000,"low_up",2600);row["firmware"]["mode"]=None
        self.assertFalse(self.s.phase_valid(row,"single"))

    def test_stable_hold_restarts_after_invalid_state_or_observation_gap(self):
        s=self.s
        for ms in range(0,60000,5000):self.assertFalse(s.held(sample(ms,"low_up",2600),True,90000))
        self.assertFalse(s.held(sample(60000,"low_up",2600),False,90000))
        for ms in range(65000,155000,5000):self.assertFalse(s.held(sample(ms,"low_up",2600),True,90000))
        self.assertTrue(s.held(sample(155000,"low_up",2600),True,90000))
        self.assertFalse(s.held(sample(175000,"low_up",2600),True,90000))

    def test_missing_phase_and_demand_reversal_cannot_pass(self):
        report=self.full_run();del self.s.result["phase_results"]["mid_down"]
        self.s.finalize(report)
        self.assertFalse(self.s.result["checks"]["mid_down"])
        self.assertFalse(self.s.result["checks"]["power_falls"])

    def test_wire_stop_between_valid_samples_cannot_hide(self):
        report=self.full_run();high=self.s.result["phase_results"]["high"]
        report["writes"].append({"unit":1,"register":3999,"values":[0],"at_ms":high["start_ms"]+20000,"exception":0})
        report["writes"].sort(key=lambda r:r["at_ms"])
        self.s.finalize(report)
        self.assertFalse(self.s.result["checks"]["stable_topology_on_wire"])

    def test_power_house_rejects_cooling_between_samples(self):
        report={"duration_s":90,"scenario":"power-house","dynamics":{"version":1,"stage":"complete"},
                "writes":[{"unit":1,"register":3999,"values":[1],"exception":0}]}
        self.assertFalse(qualify(report)[0]["heating_modes"])

    def test_profile_cannot_be_applied_to_other_routes(self):
        for scenario,climate in (("compatibility","cold"),("power-house","unknown")):
            with self.assertRaisesRegex(ValueError,"climate"):
                run(Path("missing.bin"),scenario=scenario,power_house_climate=climate)
        with self.assertRaisesRegex(ValueError,"device mode"):
            run(Path("missing.bin"),scenario="power-house",mode="baseline")

    def test_keepalive_continues_after_completion_without_reopening_phases(self):
        self.s.result["stage"]="complete";self.fleet.now_ms=10000
        self.s.power=2600
        self.s.result["post_complete"]={"begin_ms":5000,"last_valid_ms":5000,"invalid_since_ms":None}
        self.s.sample=lambda _:sample(10000,"complete",2600)
        self.s.observe(self.get)
        self.s.refresh.assert_called_once()
        self.assertEqual(self.s.result["stage"],"complete")

    def test_permanent_loss_after_completion_cannot_pass(self):
        self.s.result["stage"]="complete";self.s.power=2600
        self.s.result["post_complete"]={"begin_ms":5000,"last_valid_ms":5000,"invalid_since_ms":None}
        for ms in (10000,70000):
            self.fleet.now_ms=ms;self.s.sample=lambda _,ms=ms:sample(ms,"complete",2600,(),True)
            self.s.observe(self.get)
        self.assertEqual(self.s.result["stage"],"failed")


if __name__ == "__main__":
    unittest.main()
