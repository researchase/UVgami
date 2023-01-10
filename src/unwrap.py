# Copyright (C) 2022 Daniel Boxer
# See __init__.py and LICENSE for more information

import bpy
import bmesh
import mathutils
import pathlib
import subprocess
import functools
import platform
import collections
import threading
import numpy
import datetime
from .utils import (
    check_collection,
    check_exists,
    get_linux_path,
    get_preferences,
    import_obj,
    move_to_collection,
    get_dir_path,
    print_stdin,
)
from .handler import handle_error
from .manager import manager
from .logger import logger
from . import progress_bar


class Unwrap:
    def __init__(
        self,
        name,
        input_name,
        path,
        guide_path,
        edge_path,
        jobs,
        origin,
        materials,
        added_edges,
        vertex_count,
        shade_smooth,
        auto_smooth,
    ):
        self.name = name
        self.input_name = input_name
        self.path = path
        self.output_path = get_dir_path() / "output" / f"{self.path.stem}.obj"
        self.guide_path = guide_path
        self.edge_path = edge_path
        self.jobs = [j for j in jobs if j is not None]
        self.preserve_job = jobs[0]
        self.join_job = jobs[1]
        self.cleanup_job = jobs[2]
        self.symmetrize_job = jobs[3]
        self.origin = mathutils.Vector(origin)
        self.materials = materials
        self.added_edges = added_edges
        self.vertex_count = vertex_count
        self.shade_smooth = shade_smooth
        self.auto_smooth = auto_smooth
        self.is_active = False
        self.progress = (0, 0, 1)
        self.poll = None
        self.process = None
        self.viewer_obj = None
        self.viewing = False
        self.view_update_count = 0
        self.progress_data = collections.deque()
        self.uv_co = collections.deque()
        self.uv_indices = collections.deque()
        self.is_uv_data_ready = False
        self.is_stopped = False

    def start_unwrap(self):
        prefs = get_preferences()
        # check for valid engine
        engine_path = pathlib.Path(prefs.engine_path)
        if (
            str(engine_path) == "."
            or not engine_path.is_file()
            or engine_path.stem != "uvgami"
        ):
            engine_path = str(manager.engine_path)

        quality = bpy.context.scene.uvgami.quality
        u = ""
        if quality == "HIGH":
            u = "4.05"
        elif quality == "MEDIUM":
            u = "4.1"
        else:
            u = "4.2"

        s_weight = bpy.context.scene.uvgami.weight_value
        s = ""
        if s_weight == 5:
            s = "200"
        elif s_weight == 4:
            s = "150"
        elif s_weight == 3:
            s = "100"
        elif s_weight == 2:
            s = "50"
        elif s_weight == 1:
            s = "25"

        k = prefs.license_key
        args = []
        shared_args = f"-u {u} -s {s} -k {k} -r uvgami.com:5000"

        if platform.system() == "Windows" and engine_path.suffix == "":
            input_path = get_linux_path(self.path)
            output_path = get_linux_path(get_dir_path() / "output")
            args = [
                "bash",
                "-c",
                f"~/uvgami -i {input_path} -o {output_path}/ {shared_args}",
            ]
        else:
            args = [str(engine_path), "-i", str(self.path)] + shared_args.split()

        self.process = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stdin=subprocess.PIPE,
            universal_newlines=True,
        )

        # class functions don't register properly
        self.poll = functools.partial(self.poll_folder)
        bpy.app.timers.register(self.poll)

        # start reading thread
        thread = threading.Thread(target=self.get_output)
        thread.start()

        self.is_active = True

    def poll_folder(self):
        try:
            prefs = get_preferences()
            # check for invalid mesh
            ret_code = self.process.poll()
            if ret_code is not None and ret_code != 0:
                # mesh is invalid
                msg = ""

                move_to_invalid = False
                if ret_code == 101:
                    msg = "Non Manifold Edges"
                    move_to_invalid = True
                elif ret_code == 102:
                    msg = "Non Manifold Vertices"
                    move_to_invalid = True
                elif ret_code == 105:
                    msg = "Invalid Geometry"
                    move_to_invalid = True
                elif ret_code == 107:
                    msg = "Invalid UV Input"
                    move_to_invalid = True
                elif ret_code == 108:
                    manager.license_error = "Invalid License"
                elif ret_code == 109:
                    manager.license_error = "Can't Connect To License Server"
                elif ret_code == 110:
                    manager.license_error = "License Already Registered"
                elif ret_code == 500:
                    manager.license_error = "License Server Error"
                else:
                    manager.unknown_error = True

                if move_to_invalid:
                    if prefs.invalid_collection:
                        # move to collection for invalid meshes
                        invalid_obj = import_obj(self.path)
                        collection = check_collection(
                            "UVgami Invalid Input", bpy.context.scene.collection
                        )
                        move_to_collection(invalid_obj, collection)
                        invalid_name = f"{invalid_obj.name}: {msg}"
                        invalid_obj.name = invalid_name
                        invalid_obj.hide_set(True)
                        logger.add_data("errors", invalid_name)

                    manager.found_invalid_objects = True

                found_job = None
                # count has to be reduced because this object won't be unwrapped
                for job in self.jobs:
                    if job.count > 1:
                        job.count = job.count - 1
                        # found_job can't be a Cleanup job because the unwrapped list
                        # will be empty
                        if job.type == "JOIN":
                            found_job = job

                self.cancel_unwrap(invalid_obj=True)

                # if the invalid obj has jobs that are complete with the now reduced count
                # that means that this unwrap was the last of the group
                if found_job is not None and found_job.is_completed():
                    # use the last completed unwrap
                    manager.finish_unwrap(found_job.unwrapped[-1], invalid_pass=True)

                manager.start_next()

            # update progress bar
            if prefs.show_progress_bar:
                self.update_progress()

            # update viewer
            if self.viewing:
                self.update_viewer()

            # if part of batch unwrap, hasn't started and stop button pressed
            if self.is_stopped:
                print_stdin(self.process, "stop")

            logger.update_time()

            # make sure process has ended
            if self.output_path.is_file() and self.process.poll() is not None:
                manager.finish_unwrap(self)
                return None

        except Exception as e:
            handle_error(e, "MIDDLE")

        return 0.1

    def unregister_poll(self):
        if bpy.app.timers.is_registered(self.poll):
            bpy.app.timers.unregister(self.poll)
            del self.poll

    def stop_process(self):
        if self.process is not None and self.process.poll() is None:
            if platform.system() == "Windows" and manager.engine_path.suffix == "":
                # wsl
                print_stdin(self.process, "cancel")
            else:
                # windows
                self.process.kill()

    def get_output(self):
        # get lines until there are no more left
        for line in iter(self.process.stdout.readline, ""):
            if line.startswith("progress: "):
                self.progress_data.append(line[10:])
            elif line == "visual_begin:\n":
                self.uv_co.clear()
                self.uv_indices.clear()
                self.is_uv_data_ready = False
            elif line == "visual_end:\n":
                self.is_uv_data_ready = True
            elif line.startswith("vt"):
                uv_co = line[3:].split()
                self.uv_co.append((float(uv_co[0]), float(uv_co[1])))
            elif line.startswith("f"):
                uv_indices = line[2:].split()
                self.uv_indices.append(
                    (int(uv_indices[0]), int(uv_indices[1]), int(uv_indices[2]))
                )
        # process has ended, thread will exit here

    def update_progress(self):
        if len(self.progress_data) > 0:
            progress = self.progress_data.popleft()
            try:
                self.progress = tuple(float(num) for num in progress.split())
            except ValueError:
                # invalid progress string
                return
            self.progress_data.clear()

            # go through all unwraps to calculate total progress
            progress = [numpy.array(unwrap.progress) for unwrap in manager.active]
            # fill up progress bar with finished unwraps
            for _ in range(manager.starting_count - len(progress)):
                progress.append(numpy.array((1, 0, 0)))
            # average
            new_progress = sum(progress) / manager.starting_count

            progress_bar.update(new_progress)
            # force redraw of view3D
            bpy.context.view_layer.objects.active = (
                bpy.context.view_layer.objects.active
            )

    def update_viewer(self):
        print_stdin(self.process, "snapshot")
        if self.is_uv_data_ready:
            uvs = list(self.uv_co)
            uv_idcs = list(self.uv_indices)
            self.is_uv_data_ready = False

            # need to use from_edit_mesh here so mesh is updated in edit mode
            bm = bmesh.from_edit_mesh(self.viewer_obj.data)
            uv_map = bm.loops.layers.uv.verify()

            for face in bm.faces:
                # set uvs
                uv_idx_triple = uv_idcs[face.index]
                face.loops[0][uv_map].uv = uvs[uv_idx_triple[0]]
                face.loops[1][uv_map].uv = uvs[uv_idx_triple[1]]
                face.loops[2][uv_map].uv = uvs[uv_idx_triple[2]]

            # need to use update_edit_mesh, don't call bm.free(), it will crash
            bmesh.update_edit_mesh(self.viewer_obj.data)

    def cancel_unwrap(self, start_next=False, invalid_obj=False):
        if not invalid_obj:
            manager.cancelled_count += 1
        self.unregister_poll()
        self.stop_process()
        self.remove()
        manager.exit_viewer = True
        # update 3d view to remove progress bar
        bpy.context.view_layer.objects.active = bpy.context.view_layer.objects.active
        if start_next and self.is_active:
            manager.start_next()

    def remove(self):
        manager.active.remove(self)
        try:
            if self.path.is_file():
                self.path.unlink()
            if self.guide_path is not None and self.guide_path.is_file():
                self.guide_path.unlink()
        except PermissionError:
            logger.add_data("errors", "Error deleting file")
        if self.viewer_obj is not None and check_exists(self.viewer_obj):
            bpy.data.objects.remove(self.viewer_obj, do_unlink=True)
