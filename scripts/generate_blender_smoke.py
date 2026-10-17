"""Synthetic Blender integration test; no private game code or uploaded photos."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


def args_from_blender():
    argv = sys.argv[sys.argv.index("--")+1:] if "--" in sys.argv else []
    p = argparse.ArgumentParser()
    p.add_argument("--output-dir", type=Path, required=True)
    p.add_argument("--style", choices=["anime","semi_realistic","realistic"], default="semi_realistic")
    return p.parse_args(argv)


def main():
    import bpy
    from math import radians
    args = args_from_blender()
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)

    def material(name, rgb):
        mat = bpy.data.materials.new(name)
        mat.diffuse_color = (*rgb, 1)
        mat.use_nodes = True
        shader = mat.node_tree.nodes.get("Principled BSDF")
        shader.inputs["Base Color"].default_value = (*rgb, 1)
        return mat

    skin = material("Skin study", (.49,.28,.19))
    shirt = material("Black textile", (.025,.031,.048))
    pants = material("Trousers", (.085,.098,.12))
    shoes = material("Sneakers", (.26,.28,.32))
    eyes = material("Eye white", (.92,.92,.88))

    skeleton = bpy.data.armatures.new("3DX Human Rig")
    arm = bpy.data.objects.new("Armature_3DX", skeleton)
    bpy.context.collection.objects.link(arm)
    bpy.context.view_layer.objects.active = arm
    arm.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    specs = [
        ("root",(0,0,.94),(0,0,1.07),None),
        ("spine",(0,0,1.07),(0,0,1.46),"root"),
        ("chest",(0,0,1.46),(0,0,1.60),"spine"),
        ("neck",(0,0,1.60),(0,0,1.72),"chest"),
        ("head",(0,0,1.72),(0,0,1.98),"neck"),
        ("arm.L",(.26,0,1.52),(.63,0,1.50),"chest"),
        ("forearm.L",(.63,0,1.50),(.96,0,1.48),"arm.L"),
        ("arm.R",(-.26,0,1.52),(-.63,0,1.50),"chest"),
        ("forearm.R",(-.63,0,1.50),(-.96,0,1.48),"arm.R"),
        ("thigh.L",(.16,0,.98),(.16,0,.51),"root"),
        ("shin.L",(.16,0,.51),(.16,0,.11),"thigh.L"),
        ("foot.L",(.16,0,.11),(.16,-.17,.06),"shin.L"),
        ("thigh.R",(-.16,0,.98),(-.16,0,.51),"root"),
        ("shin.R",(-.16,0,.51),(-.16,0,.11),"thigh.R"),
        ("foot.R",(-.16,0,.11),(-.16,-.17,.06),"shin.R"),
    ]
    for name, head, tail, _ in specs:
        bone = skeleton.edit_bones.new(name)
        bone.head, bone.tail = head, tail
    for name, _, _, parent in specs:
        if parent:
            skeleton.edit_bones[name].parent = skeleton.edit_bones[parent]
    bpy.ops.object.mode_set(mode="OBJECT")
    arm.select_set(False)

    head_factor = {"anime":1.27, "semi_realistic":1.05, "realistic":1.0}[args.style]
    meshes = []
    def sphere(name, location, scale, mat, bone):
        bpy.ops.mesh.primitive_uv_sphere_add(segments=16, ring_count=10, location=location)
        obj = bpy.context.object
        obj.name = name
        obj.scale = scale
        obj.data.materials.append(mat)
        bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
        group = obj.vertex_groups.new(name=bone)
        group.add(list(range(len(obj.data.vertices))), 1.0, "REPLACE")
        modifier = obj.modifiers.new("3DX Armature", "ARMATURE")
        modifier.object = arm
        obj.parent = arm
        meshes.append(obj)
        return obj

    sphere("Torso",(0,0,1.34),(.30,.175,.37),shirt,"spine")
    sphere("Hips",(0,0,1.00),(.26,.18,.19),pants,"root")
    sphere("Head",(0,0,1.81),(.18*head_factor,.15*head_factor,.22*head_factor),skin,"head")
    sphere("Hair cap",(0,0,1.94),(.177*head_factor,.15*head_factor,.105*head_factor),shirt,"head")
    for side, sign in (("L",1),("R",-1)):
        sphere("Upper arm "+side,(sign*.45,0,1.51),(.24,.097,.103),shirt,"arm."+side)
        sphere("Forearm "+side,(sign*.79,0,1.49),(.20,.08,.082),skin,"forearm."+side)
        sphere("Hand "+side,(sign*.99,0,1.48),(.095,.075,.075),skin,"forearm."+side)
        sphere("Thigh "+side,(sign*.16,0,.78),(.119,.13,.30),pants,"thigh."+side)
        sphere("Shin "+side,(sign*.16,0,.30),(.095,.10,.24),pants,"shin."+side)
        sphere("Shoe "+side,(sign*.16,-.10,.08),(.122,.23,.085),shoes,"foot."+side)
        sphere("Eye "+side,(sign*.07,-.139,1.83),(.042,.02,.026),eyes,"head")

    arm.animation_data_create()
    action = bpy.data.actions.new("Idle_Smoke_Test")
    arm.animation_data.action = action
    for frame, angle in ((1,0),(16,radians(1.3)),(32,0)):
        bpy.context.scene.frame_set(frame)
        pose = arm.pose.bones["spine"]
        pose.rotation_mode = "XYZ"
        pose.rotation_euler[0] = angle
        pose.keyframe_insert(data_path="rotation_euler", frame=frame, group="spine")
    bpy.context.scene.frame_start = 1
    bpy.context.scene.frame_end = 32
    bpy.context.scene.render.fps = 24

    bpy.ops.wm.save_as_mainfile(filepath=str(output / "3dx-character.blend"))
    bpy.ops.export_scene.gltf(filepath=str(output / "3dx-character.glb"),
                              export_format="GLB", export_animations=True,
                              export_skins=True, export_apply=False, use_selection=False)
    report = {"schema":"3dx-public-ci-smoke/1", "style":args.style,
              "mesh_objects":len(meshes), "rig_bones":len(specs),
              "private_assets_used":False, "intent":"CI regression test, not production"}
    (output/"3dx-generator-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print("3DX Blender smoke test completed:", json.dumps(report))


if __name__ == "__main__":
    main()
